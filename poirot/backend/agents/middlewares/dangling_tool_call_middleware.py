"""DanglingToolCallMiddleware — 在 resume 时修补悬空的工具调用。

【背景问题】
当 graph 因为 help request 或用户中断（interrupt）而恢复（resume）时，
历史里最后一条 AIMessage 可能带有一批 tool_calls，但并没有对应的
ToolMessage。这些「有 call 没有 result」的调用就是 dangling calls。
消息历史不完整会让严格后端直接返回 LLM 400 错误。

【整体职责】
在每次进入 model 之前（before_model）扫描消息历史，
为每个 dangling 的 tool_call 补一条「占位用的错误 ToolMessage」，
让 tool pairing 重新变得完整。

【模式来源】
借鉴 deer-flow DanglingToolCallMiddleware 模式。
"""

from __future__ import annotations

import json
from typing import Any, override

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.runtime import Runtime

from poirot.backend.agents.state.types import ThreadState

_MAX_RECOVERY_DETAIL_LEN = 500


class DanglingToolCallMiddleware(AgentMiddleware):
    """在模型调用前，为悬空 tool call 注入占位 ToolMessage。

    只处理「AIMessage 里声明了 tool_call，但消息历史中没有对应
    ToolMessage」的情况；已配对的调用不动。
    """

    state_schema = ThreadState  # type: ignore[assignment]

    @staticmethod
    def _extract_tool_calls(msg: AIMessage) -> list[dict[str, Any]]:
        """从 AIMessage 中提取 tool_calls（兼容两种存储形态）。

        优先取标准字段 msg.tool_calls（LangChain 规范位置）。
        若该字段为空，再退回 additional_kwargs["tool_calls"]（OpenAI 原始
        形态）解析，把每条 raw tool_call 归一化为
        {"id", "name", "args"} 结构。

        解析细节：
        - raw 项非 dict → 跳过；
        - name：先取 rtc["name"]，其次 rtc["function"]["name"]，兜底 "unknown"；
        - args：先取 rtc["args"]；为空且 function.arguments 存在时，
          尝试 json.loads（仅当其为 str），失败则用 {}。

        Args:
            msg: 待解析的 AIMessage。

        Returns:
            归一化后的 tool_call 列表，每项含 id / name / args。
        """
        calls = list(getattr(msg, "tool_calls", None) or [])
        raw = (getattr(msg, "additional_kwargs", None) or {}).get("tool_calls") or []
        if not calls:
            for rtc in raw:
                if not isinstance(rtc, dict):
                    continue
                fn = rtc.get("function", {})
                name = rtc.get("name") or fn.get("name", "unknown")
                args = rtc.get("args", {})
                if not args and isinstance(fn, dict):
                    raw_args = fn.get("arguments", "{}")
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                calls.append({"id": rtc.get("id", ""), "name": name, "args": args})
        return calls

    @override
    def before_model(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:
        """同步 before_model：扫描历史 → 找出 dangling call → 注入占位 ToolMessage。

        处理流程：
        1. 取 state.messages；为空直接返回 None（无内容可扫描）。
        2. 第一遍扫描：把所有 ToolMessage 的 tool_call_id 收集到
           answered_ids（即「已有结果」的调用 id 集合）。
        3. 第二遍扫描：对每条 AIMessage：
           - 用 _extract_tool_calls 取归一化后的 tool_calls；
           - 对每个 tc：若其 id 非空且不在 answered_ids 中，
             则补一条 ToolMessage：
               content = "[Tool call was interrupted and did not return a result.]"
               tool_call_id = tc.id
               name = tc.name（缺省 "unknown"）；
             并把该 id 加入 answered_ids，避免后续重复补。
        4. 若 patch 为空（无 dangling call）→ 返回 None（不写 state）。
        5. 否则返回 {"messages": patch}，由框架并入 state.messages。

        注意：本 hook 只补「占位结果」，不重放工具、不改其他 state 字段。

        Args:
            state:   当前 ThreadState，读取 messages。
            runtime: LangGraph 运行时（本 hook 未使用）。

        Returns:
            含补丁 messages 的 state patch；无 dangling call 时返回 None。
        """
        messages = state.get("messages") or []
        if not messages:
            return None

        patch: list[ToolMessage] = []
        answered_ids: set[str] = set()
        for msg in messages:
            if isinstance(msg, ToolMessage):
                answered_ids.add(msg.tool_call_id)

        for msg in messages:
            if not isinstance(msg, AIMessage):
                continue
            calls = self._extract_tool_calls(msg)
            for tc in calls:
                tc_id = tc.get("id", "")
                if tc_id and tc_id not in answered_ids:
                    patch.append(ToolMessage(
                        content="[Tool call was interrupted and did not return a result.]",
                        tool_call_id=tc_id,
                        name=tc.get("name", "unknown"),
                    ))
                    answered_ids.add(tc_id)

        if not patch:
            return None
        return {"messages": patch}

    @override
    async def abefore_model(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:
        """异步 before_model：直接转调同步版，保证行为一致。

        Args:
            state:   当前 ThreadState。
            runtime: LangGraph 运行时。

        Returns:
            与 before_model 相同的 state patch（或 None）。
        """
        return self.before_model(state, runtime)