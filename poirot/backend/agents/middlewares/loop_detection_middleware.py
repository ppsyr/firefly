"""LoopDetectionMiddleware — ReAct 死循环熔断器。

【要解决的问题】
default 模式没有 Todo 完成度强制时，模型可能陷入死循环：
反复用同一个工具、同一组参数调用，白白烧 token，直到 recursion_limit 耗尽
才被迫停下。本中间件在 after_model 检测这种重复调用，超阈时熔断：

1. 清掉最后一条 AIMessage 的 tool_calls；
2. 注入一条终止引导（隐藏 HumanMessage）；
3. jump_to="model"，强制模型基于已有信息收尾。

【启用范围】
全模式挂载（default + expert）。

【思路来源】
借鉴 deer-flow LoopDetectionMiddleware 的思路。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, override

from langchain.agents.middleware.types import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.runtime import Runtime

from poirot.backend.agents.state.types import ThreadState


@dataclass(frozen=True)
class LoopDetectionConfig:
    """LoopDetection 配置。

    字段：
        enabled:   是否启用检测。
        window:    扫描最近 N 条消息。
        threshold: 同 (tool_name, args_hash) 出现 M 次即触发熔断。
    """

    enabled: bool = True
    window: int = 10   # 扫描最近 N 条消息
    threshold: int = 3 # 同 (tool, args) 出现 M 次触发


def _hash_args(args: Any) -> str:
    """对工具参数做哈希（用于比对是否「同参数」）。

    处理流程：
    1. 尝试 json.dumps(args, sort_keys=True, ensure_ascii=False)，
       截断到前 100 字符。
    2. 序列化失败（TypeError / ValueError）→ str(args) 截断 100 字符。

    为什么截断 100 字：
    避免微小的参数差异（比如尾部不同）逃过检测；用 sort_keys 保证
    键顺序不影响哈希结果。

    Args:
        args: 工具调用参数。

    Returns:
        哈希字符串（≤100 字符）。
    """
    try:
        return json.dumps(args, sort_keys=True, ensure_ascii=False)[:100]
    except (TypeError, ValueError):
        return str(args)[:100]


def _detect_loop(
    messages: list[Any],
    window: int = 10,
    threshold: int = 3,
) -> str | None:
    """扫描最近 window 条消息，找重复的 (tool_name, args_hash)。

    处理流程：
    1. 取最近 window 条（window <= 0 时取全部）。
    2. 遍历每条 AIMessage.tool_calls，对每个 tc：
       - name = tc["name"]；
       - args = tc["args"]；
       - key = (name, _hash_args(args))；
       - call_counts[key] += 1。
    3. 遍历 call_counts，若有任意 key 的 count >= threshold
       → 返回该 key 的 tool_name。
    4. 无命中 → None。

    Args:
        messages:  消息列表。
        window:    扫描窗口（最近 N 条）。
        threshold: 触发阈值。

    Returns:
        重复的 tool_name；无重复返回 None。
    """
    recent = messages[-window:] if window > 0 else messages
    call_counts: dict[tuple[str, str], int] = {}
    for msg in recent:
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                if not isinstance(tc, dict):
                    continue
                name = tc.get("name", "") or ""
                args = tc.get("args", {}) or {}
                key = (name, _hash_args(args))
                call_counts[key] = call_counts.get(key, 0) + 1
    for (name, _), count in call_counts.items():
        if count >= threshold:
            return name
    return None


def _build_guidance(loop_tool: str) -> str:
    """构造终止引导文本（告诉模型「别再重复调这个工具了，给答案吧」）。

    Args:
        loop_tool: 被检测到循环调用的工具名。

    Returns:
        包在 <system_reminder> 里的引导文本。
    """
    return (
        "<system_reminder>\n"
        f"检测到工具 {loop_tool} 重复调用循环。"
        "请基于已有信息给出最终答案，不要再重复调用该工具。\n"
        "</system_reminder>"
    )


class LoopDetectionMiddleware(AgentMiddleware):
    """ReAct 死循环熔断器。

    after_model 检测重复工具调用，超阈时：
    - 清最后一条 AIMessage 的 tool_calls；
    - 注入终止引导；
    - jump_to="model"。

    只挂 after_model（同步 + 异步），不干扰工具调用本身。
    """

    state_schema = ThreadState  # type: ignore[assignment]

    def __init__(self, config: LoopDetectionConfig | None = None) -> None:
        """初始化。

        Args:
            config: 检测配置；为 None 时使用默认 LoopDetectionConfig()。
        """
        self._config = config or LoopDetectionConfig()

    @hook_config(can_jump_to=["model"])
    @override
    def after_model(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:
        """同步 after_model：检测循环 → 清 tool_calls → 注入引导 → jump_to="model"。

        处理流程：
        1. 若 config.enabled 为 False → return None。
        2. 取 messages；调 _detect_loop 扫描最近 window 条，找重复
           (tool_name, args_hash) 达到 threshold 的 tool_name。
        3. 无循环 → return None。
        4. 取最后一条 AIMessage；若不存在或无 tool_calls → return None。
        5. 构造 cleared AIMessage（替换原消息）：
           - id 沿用原 AIMessage.id（关键：让 add_messages 按 id 替换而非追加）；
           - content 保留原 content（或空串）；
           - tool_calls 清空；
           - additional_kwargs：
             · 剥掉 tool_calls / function_call（防 _has_tool_call_intent 误判）；
             · 加 loop_detected = loop_tool（标记这条消息被熔断过）。
        6. 构造 guidance HumanMessage（name="loop_detection"，
           hide_from_ui=True，内容为 _build_guidance(loop_tool)）。
        7. 返回 {"messages": [cleared, guidance], "jump_to": "model"}。

        为什么用「复用原 id」而不是追加新消息：
        若只追加一条 cleared 消息，原 AIMessage 的 tool_calls 仍留在历史里，
        jump_to="model" 会跳过 ToolNode，导致没有 ToolMessage 配对，
        下一轮 model 调用直接 400: insufficient tool messages following tool_calls。
        复用 id 让 add_messages 把原消息替换掉，历史里就不再有悬空 tool_call。

        为什么剥 additional_kwargs.tool_calls / function_call：
        _has_tool_call_intent 会检查这两个字段；若不清，即便 tool_calls=[] 了，
        也会被误判为「还想调工具」，导致后续逻辑走错分支。

        Args:
            state:   当前 ThreadState，读取 messages。
            runtime: LangGraph 运行时（本 hook 未使用）。

        Returns:
            含 messages 替换 + jump_to="model" 的 state patch；无循环时返回 None。
        """
        if not self._config.enabled:
            return None
        messages = state.get("messages") or []
        loop_tool = _detect_loop(messages, self._config.window, self._config.threshold)
        if loop_tool is None:
            return None

        last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
        if not last_ai or not getattr(last_ai, "tool_calls", None):
            return None

        # 清 tool_calls 强制模型给最终答案，保留 content + 标记 loop_detected。
        # 复用原 id → add_messages 按 id 替换原消息（非追加），否则原 AIMessage 的
        # tool_calls 仍留 history，jump_to model 跳过 ToolNode 后无 ToolMessage 配对 →
        # 下一轮 model 调用 400: insufficient tool messages following tool_calls。
        # 同步剥 additional_kwargs.tool_calls/function_call 防 _has_tool_call_intent 误判。
        new_kwargs = {
            k: v for k, v in (last_ai.additional_kwargs or {}).items()
            if k not in ("tool_calls", "function_call")
        }
        new_kwargs["loop_detected"] = loop_tool
        cleared = AIMessage(
            id=last_ai.id,
            content=last_ai.content or "",
            tool_calls=[],
            additional_kwargs=new_kwargs,
        )
        guidance = HumanMessage(
            name="loop_detection",
            additional_kwargs={"hide_from_ui": True},
            content=_build_guidance(loop_tool),
        )
        return {"messages": [cleared, guidance], "jump_to": "model"}

    @hook_config(can_jump_to=["model"])
    @override
    async def aafter_model(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:
        """异步 after_model：直接转调同步版，保证行为一致。"""
        return self.after_model(state, runtime)