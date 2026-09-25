"""OrchestrationMiddleware — 横切打点 + 产物汇总。

【整体职责】
在工具调用层拦截 delegate_to_* 这类"委派给 specialist"的调用，做两件事：
1. 打点：在调用前后维护 multiagent 的四个计数器
   （selection / invoked / completion / fallback）。
2. 产物汇总：把 specialist 返回的 artifacts 归一化为 ArtifactRef 列表，
   写进 ThreadState.orchestration（active_specialists + specialist_artifacts）。

【内容摘要】
- _tool_text()                     : 把工具结果压平成字符串（供 success 判定 / artifacts 解析）。
- OrchestrationMiddleware          : 中间件主类，实现 wrap_tool_call / awrap_tool_call。
- _is_delegate_tool()              : 判断是否委派类工具（名字以 delegate_to_ 开头）。
- _extract_specialist_name()       : 从工具名剥出 specialist 名。
- _is_success()                    : 从结果 JSON 判定成败。
- _build_orchestration_update()    : 构造写入 ThreadState.orchestration 的更新。
- _before_handler()                : handler 前的 state 暴露 + selection/invoked 打点。
- _after_handler()                 : handler 后的 completion/fallback 打点 + orchestration 构造。
- _make_error_response()           : specialist 抛异常时合成 error ToolMessage 的 Command。
- wrap_tool_call / awrap_tool_call : 同步 / 异步拦截入口。

【职责边界】
- 只负责：横切打点、产物汇总、异常转 ToolMessage。
- 不负责：选择哪个 specialist（由 LLM 自行决定，soft routing）、
  specialist 实际执行（runtime 负责）、L2 触发与预算检查
  （l2_trigger_middleware / budget_guard 预留，本文件未使用）。
- 非委派工具直接透传，不做任何处理。

【INVARIANT】
- 不做编排决策：路由由 LLM 决定，本中间件不参与选择。
- pairing 完整性：specialist 失败时转成 error ToolMessage，保证
  AIMessage(tool_calls) → ToolMessage 配对，否则下一轮 model 调用会 400。
- 打点顺序固定：before → selection + invoked；after → completion 或 fallback。
- 异常路径也打点：handler 抛异常时先 record_fallback，再返回 error Command。
- 挂载位置：在 ToolCallMiddleware 之后。
- 同步 / 异步两个入口逻辑必须保持一致，仅 handler 调用方式不同（await）。
- 只有 delegate_to_* 工具被拦截，其余工具 passthrough。
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, override

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from poirot.backend.agents.multiagent.metrics import MultiAgentMetricsStore
from poirot.backend.agents.multiagent.tools import set_current_state
from poirot.backend.agents.multiagent.types import ArtifactRef
from poirot.backend.agents.state.types import ThreadState


def _tool_text(result: Any) -> str:
    """把工具结果压平成字符串，供 success 判定与 artifacts 解析使用。

    - ToolMessage：content 为 str 时原样返回；为 list 时逐项拼接
      （dict 取 "text"，否则 str(item)），跳过假值项；其他类型 str(content)。
    - Command：取其 update.messages[0] 递归 _tool_text；无 messages 时退回 str。
    - 其他对象：str(result)。

    Args:
        result: 工具调用结果。

    Returns:
        str: 压平后的字符串。
    """
    if isinstance(result, ToolMessage):
        content = result.content
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                item.get("text", "") if isinstance(item, dict) else str(item)
                for item in content if item
            )
        return str(content)
    if isinstance(result, Command):
        messages = result.update.get("messages", [])
        if messages:
            return _tool_text(messages[0])
    return str(result)


class OrchestrationMiddleware(AgentMiddleware):
    """横切打点 + 产物汇总中间件。

    拦截 delegate_to_* 工具调用：
    - handler 前：set_current_state + record_selection + record_invoked
    - handler 后：record_completion / record_fallback + 写 orchestration state
    - specialist 失败：转 error ToolMessage（保证 pairing 完整性）

    非 delegate_to_* 工具直接透传。

    Attributes:
        state_schema: 状态 schema（ThreadState）。
        _metrics: multiagent 指标存储。
        _l2_trigger_middleware: L2 触发中间件（预留，未使用）。
        _budget_guard: 预算守卫（预留，未使用）。
    """

    state_schema = ThreadState  # type: ignore[assignment]

    def __init__(
        self,
        metrics_store: MultiAgentMetricsStore | None = None,
        l2_trigger_middleware: Any | None = None,
        budget_guard: Any | None = None,
    ) -> None:
        """初始化。

        Args:
            metrics_store: multiagent 指标存储，用于四个计数器打点；可为 None（不打点）。
            l2_trigger_middleware: L2 触发中间件，本文件未使用（预留）。
            budget_guard: 预算守卫，本文件未使用（预留）。
        """
        self._metrics = metrics_store
        self._l2_trigger_middleware = l2_trigger_middleware
        self._budget_guard = budget_guard

    def _is_delegate_tool(self, tool_name: str) -> bool:
        """判断是否为委派类工具（名字以 "delegate_to_" 开头）。"""
        return tool_name.startswith("delegate_to_")

    def _extract_specialist_name(self, tool_name: str) -> str:
        """从 "delegate_to_X" 中剥出 specialist 名 X。"""
        return tool_name.removeprefix("delegate_to_")

    def _is_success(self, result: Any) -> bool:
        """判断 specialist 结果是否成功。

        把结果压平成文本后尝试 json.loads，取 "success" 字段（bool）。
        解析失败（非法 JSON / 类型不对）视为不成功。

        Args:
            result: specialist 工具返回结果。

        Returns:
            bool: 是否成功。
        """
        text = _tool_text(result)
        try:
            data = json.loads(text)
            return bool(data.get("success", False))
        except (json.JSONDecodeError, TypeError):
            return False

    def _build_orchestration_update(
        self,
        specialist_name: str,
        result: Any,
    ) -> dict[str, Any]:
        """构造要写入 ThreadState.orchestration 的更新。

        从结果 JSON 的 "artifacts" 列表构造 ArtifactRef（path / type /
        specialist_name）。解析失败时 artifacts 为空列表。

        Args:
            specialist_name: 本次调用的 specialist 名。
            result: specialist 工具返回结果。

        Returns:
            dict[str, Any]: {"active_specialists": [specialist_name],
                "specialist_artifacts": [ArtifactRef, ...]}。
        """
        artifacts: list[ArtifactRef] = []
        text = _tool_text(result)
        try:
            data = json.loads(text)
            for a in data.get("artifacts", []):
                artifacts.append(
                    ArtifactRef(
                        path=a.get("path", ""),
                        artifact_type=a.get("type", ""),
                        specialist_name=specialist_name,
                    )
                )
        except (json.JSONDecodeError, TypeError):
            pass
        return {
            "active_specialists": [specialist_name],
            "specialist_artifacts": artifacts,
        }

    def _before_handler(
        self,
        request: ToolCallRequest,
        specialist_name: str,
    ) -> None:
        """handler 执行前的准备：暴露 state + 打 selection / invoked 两点。

        处理流程：
        1. 取 request.state（非 dict 时用空 dict），调 set_current_state 暴露给 tool handler。
        2. 若 metrics_store 存在：record_selection + record_invoked。

        Args:
            request: 工具调用请求。
            specialist_name: 本次调用的 specialist 名。
        """
        state = request.state if isinstance(request.state, dict) else {}
        set_current_state(state)
        if self._metrics:
            self._metrics.record_selection(specialist_name)
            self._metrics.record_invoked(specialist_name)

    def _after_handler(
        self,
        specialist_name: str,
        result: Any,
    ) -> dict[str, Any]:
        """handler 成功返回后的处理：打 completion / fallback + 构造 orchestration。

        处理流程：
        1. _is_success(result) 判定成败。
        2. 若 metrics_store 存在：成功 → record_completion；失败 → record_fallback。
        3. _build_orchestration_update 构造 orchestration 更新并返回。

        Args:
            specialist_name: 本次调用的 specialist 名。
            result: specialist 工具返回结果。

        Returns:
            dict[str, Any]: orchestration 更新 dict。
        """
        success = self._is_success(result)
        if self._metrics:
            if success:
                self._metrics.record_completion(specialist_name)
            else:
                self._metrics.record_fallback(specialist_name)
        return self._build_orchestration_update(specialist_name, result)

    def _make_error_response(
        self,
        request: ToolCallRequest,
        exc: Exception,
        specialist_name: str,
    ) -> Command:
        """specialist 抛异常时，合成 error ToolMessage + orchestration 的 Command。

        处理流程：
        1. 取 tool_call id。
        2. 构造 error ToolMessage：content = JSON{success=False, error:{type, message},
           suggestion}，status="error"。
        3. orchestration = {active_specialists: [name], specialist_artifacts: []}。
        4. 返回 Command(update={messages: [error_msg], orchestration: orch})。

        目的是保证 AIMessage(tool_calls) 后面必有对应 ToolMessage，
        否则下一轮 model 调用会 400（pairing 完整性）。

        Args:
            request: 工具调用请求（取 tool_call id）。
            exc: 捕获到的异常。
            specialist_name: 本次调用的 specialist 名。

        Returns:
            Command: 带 error ToolMessage 与 orchestration 的 Command。
        """
        call_id = request.tool_call.get("id", "")
        error_msg = ToolMessage(
            content=json.dumps({
                "success": False,
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
                "suggestion": "retry, fallback to another specialist, or self-do",
            }),
            tool_call_id=call_id,
            status="error",
        )
        orch = {"active_specialists": [specialist_name], "specialist_artifacts": []}
        return Command(update={"messages": [error_msg], "orchestration": orch})

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        """同步 wrap_tool_call：拦截 delegate_to_*，打点 + 汇总；其余透传。

        处理流程：
        1. 取 tool_name；非 delegate_to_* → handler(request) 直接透传。
        2. 剥出 specialist_name；调 _before_handler（暴露 state + 打 selection/invoked）。
        3. try: result = handler(request)
           except: record_fallback + _make_error_response → 返回 Command。
        4. orch_update = _after_handler(...)（打 completion/fallback + 构造更新）。
        5. result 是 Command → 合并 orchestration 后返回新 Command；
           否则包成 Command(update={messages: [result], orchestration: orch_update})。

        Args:
            request: 工具调用请求。
            handler: 下游处理函数。

        Returns:
            Any: delegate_to_* 时返回 Command（带 messages / orchestration）；
                其他工具返回 handler 结果。
        """
        tool_name = request.tool_call.get("name", "")
        if not self._is_delegate_tool(tool_name):
            return handler(request)

        specialist_name = self._extract_specialist_name(tool_name)
        self._before_handler(request, specialist_name)

        try:
            result = handler(request)
        except Exception as exc:
            if self._metrics:
                self._metrics.record_fallback(specialist_name)
            return self._make_error_response(request, exc, specialist_name)

        orch_update = self._after_handler(specialist_name, result)

        if isinstance(result, Command):
            merged = dict(result.update)
            merged["orchestration"] = orch_update
            return Command(update=merged)

        return Command(update={"messages": [result], "orchestration": orch_update})

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """异步 wrap_tool_call：逻辑与同步版一致，仅 handler 改为 await。

        处理流程：
        1. 非 delegate_to_* → await handler(request) 透传。
        2. _before_handler（暴露 state + 打 selection/invoked）。
        3. await handler(request)：
           异常 → record_fallback + _make_error_response → Command；
           成功 → _after_handler → orch_update。
        4. result 是 Command → 合并 orchestration；否则包 Command 返回。

        Args:
            request: 工具调用请求。
            handler: 下游异步处理函数。

        Returns:
            Any: delegate_to_* 时返回 Command（带 messages / orchestration）；
                其他工具返回 handler 结果。
        """
        tool_name = request.tool_call.get("name", "")
        if not self._is_delegate_tool(tool_name):
            return await handler(request)

        specialist_name = self._extract_specialist_name(tool_name)
        self._before_handler(request, specialist_name)

        try:
            result = await handler(request)
        except Exception as exc:
            if self._metrics:
                self._metrics.record_fallback(specialist_name)
            return self._make_error_response(request, exc, specialist_name)

        orch_update = self._after_handler(specialist_name, result)

        if isinstance(result, Command):
            merged = dict(result.update)
            merged["orchestration"] = orch_update
            return Command(update=merged)

        return Command(update={"messages": [result], "orchestration": orch_update})