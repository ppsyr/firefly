"""MCP per-tool-call 审计中间件 — 熔断器 + 审计 + 外化联动标记。

【整体职责】
在每次工具调用前后做三件事：
1. 熔断器检查：若该工具的 breaker 处于 open 状态，则不调用工具，
   直接返回一条 error ToolMessage，并记 circuit_open 事件。
2. 审计打点：把这次工具调用的关键信息（工具名、来源、参数摘要、状态、
   耗时、错误、是否外化）写入 journal（thread-events.jsonl）。
3. 成功/失败联动熔断器：成功 record_success，失败 record_failure，
   并在失败时对错误信息做凭证脱敏后再回 LLM。

【内容摘要】
- _truncate        : 文本截断（超出加省略提示）。
- _summarize_args  : tool 入参摘要（截断防日志爆炸）。
- McpAuditMiddleware.__init__    : 接收 registry（工具条目）与 sanitizer（脱敏）。
- McpAuditMiddleware._journal    : 从 runtime 取 journal。
- McpAuditMiddleware._emit       : 写 journal 事件（静默容错）。
- McpAuditMiddleware.awrap_tool_call : 熔断检查 → 调工具 → 审计 → 联动熔断器。
- McpAuditMiddleware._audit      : 写 tool.call 事件。

【职责边界】
- 只负责：熔断器检查与联动、审计打点、错误脱敏、外化标记识别。
- 不负责：工具的实际执行（handler）、熔断器的实现（mcp 子系统）、
  凭证脱敏规则（CredentialSanitizer）、外化的产生（tagged_context_middleware）。

【INVARIANT（必须保持的不变量）】
- awrap_tool_call 拦截所有工具调用（MCP / builtin / sandbox 统一走这里）。
- 熔断器检查：open 时不调用工具，记 circuit_open 事件。
- 成功：record_success + 记 ok 事件。
- 失败：record_failure + sanitize_error + 记 error 事件。
- 外化联动：result 含 POIROT_EXTERNALIZED 标记时，事件里加
  externalized=true + externalized_path。
- 凭证脱敏：error_text 经 CredentialSanitizer 清洗后才回 LLM / 写日志。
- 无 journal 时静默不记（不报错），容忍运行环境缺件。
- 异常向上抛（不吞），交给上层 ToolCallMiddleware 转 ToolMessage。
- 本中间件不修改成功的工具结果本身。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.runtime import Runtime
from langgraph.types import Command

from poirot.backend.agents.middlewares.run_journal_middleware import _get_runtime_value
from poirot.backend.agents.middlewares.tagged_context_middleware import (
    POIROT_EXTERNALIZED,
    POIROT_EXTERNALIZED_PATH,
)
from poirot.backend.agents.mcp.guards.credential_sanitizer import CredentialSanitizer
from poirot.backend.agents.mcp.registry import ToolRegistry

logger = logging.getLogger(__name__)

_BASH_OUTPUT_MAX_CHARS = 10000  # 单条文本截断上限（防止日志/消息爆炸）


def _truncate(text: str, max_chars: int = _BASH_OUTPUT_MAX_CHARS) -> str:
    """把文本截断到 max_chars，超出部分以省略提示替代。

    若原文长度不超过 max_chars，原样返回；否则保留前 max_chars 字符，
    并追加 "\\n... (truncated, N chars omitted)"，其中 N 为被省略的字符数。

    Args:
        text: 待截断文本。
        max_chars: 允许的最大字符数，默认 _BASH_OUTPUT_MAX_CHARS。

    Returns:
        str: 截断后的文本（可能附带省略提示）。
    """
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... (truncated, {len(text) - max_chars} chars omitted)"


def _summarize_args(args: Any) -> str:
    """生成 tool 入参的摘要字符串（截断防日志爆炸）。

    尝试用 json.dumps 序列化 args（ensure_ascii=False，default=str 兜底），
    再经 _truncate 截到 200 字符。序列化失败时退回 str(args) 再截断。

    Args:
        args: tool 调用入参。

    Returns:
        str: 截断到 200 字符的摘要字符串。
    """
    try:
        import json
        s = json.dumps(args, ensure_ascii=False, default=str)
        return _truncate(s, 200)
    except Exception:
        return _truncate(str(args), 200)


class McpAuditMiddleware(AgentMiddleware):
    """per-tool-call 审计 + 熔断器联动。

    INVARIANT:
    - 拦截所有工具调用，统一记 tool.call 事件到 thread-events.jsonl。
    - 熔断器 open 时不调用工具，记 circuit_open 事件。
    - 凭证脱敏：错误信息经 CredentialSanitizer 清洗后回 LLM。
    - 外化联动：result 含 POIROT_EXTERNALIZED 标记时加 externalized 字段。
    - 无 journal 时静默不记（不报错）。

    Attributes:
        _registry: 工具注册表（含 breaker / source）。
        _sanitizer: 凭证清洗器。
    """

    def __init__(
        self,
        registry: ToolRegistry,
        sanitizer: CredentialSanitizer | None = None,
    ) -> None:
        """初始化。

        Args:
            registry: 工具注册表，提供 get(tool_name) 取工具条目（含 breaker / source）。
            sanitizer: 凭证清洗器；为 None 时使用默认 CredentialSanitizer。
        """
        self._registry = registry
        self._sanitizer = sanitizer or CredentialSanitizer()

    def _journal(self, runtime: Runtime) -> Any:
        """从 runtime 取 journal（无则 None）。"""
        return _get_runtime_value(runtime, "journal", None)

    def _emit(self, runtime: Runtime, event_type: str, payload: dict) -> None:
        """向 journal 追加一条事件；无 journal 或 append 失败时静默跳过。

        Args:
            runtime: LangGraph 运行时，用于取 journal。
            event_type: 事件类型（如 "tool.call"）。
            payload: 事件内容。
        """
        journal = self._journal(runtime)
        if journal is not None:
            try:
                journal.append(event_type, payload)
            except Exception as exc:
                logger.debug("journal append failed: %s", exc)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """异步 wrap_tool_call：熔断器检查 → 调工具 → 审计 → 联动熔断器。

        处理流程：
        1. 从 request 取 tool_call / tool_name / runtime。
        2. 从 registry 取 entry（工具条目），source 取其 source，取不到用 "unknown"。
        3. 记录起始时间 start。
        4. 熔断器检查：若 entry 存在且 entry.breaker.allow_call() 为 False：
           - 写 circuit_open 审计事件；
           - 返回一条 status="error" 的 ToolMessage（提示熔断，建议替代方案）；
           - 不再调用 handler。
        5. 调用 handler(request)：
           - 成功：若 entry 存在则 record_success；写 ok 审计事件；返回 result。
           - 抛异常：若 entry 存在则 record_failure；对错误信息做
             sanitize_error 脱敏；写 error 审计事件（带脱敏后的 error）；
             重新抛出，让上层 middleware（ToolCallMiddleware）转成 ToolMessage。

        注意：本中间件只负责熔断 + 审计 + 联动，不修改成功的工具结果本身。

        Args:
            request: 工具调用请求（含 tool_call / runtime）。
            handler: 下游异步处理函数。

        Returns:
            Any: handler 返回的结果；熔断时返回 error ToolMessage；异常向上抛。
        """
        tool_call = request.tool_call
        tool_name = tool_call.get("name", "")
        runtime = request.runtime

        entry = self._registry.get(tool_name)
        source = entry.source if entry else "unknown"

        start = time.time()

        # 熔断器检查
        if entry and not entry.breaker.allow_call():
            self._audit(
                runtime, tool_name, source, tool_call, None,
                "circuit_open", start,
            )
            return ToolMessage(
                content=f"tool {tool_name} unavailable (circuit open), try alternative",
                tool_call_id=tool_call.get("id", ""),
                status="error",
            )

        try:
            result = await handler(request)
            if entry:
                entry.breaker.record_success()
            self._audit(
                runtime, tool_name, source, tool_call, result,
                "ok", start,
            )
            return result
        except Exception as exc:
            if entry:
                entry.breaker.record_failure()
            sanitized = self._sanitizer.sanitize_error(str(exc))
            self._audit(
                runtime, tool_name, source, tool_call, None,
                "error", start, error=sanitized,
            )
            # 重新抛出，让上层 middleware 处理（ToolCallMiddleware 会转 ToolMessage）
            raise

    def _audit(
        self,
        runtime: Runtime,
        tool_name: str,
        source: str,
        tool_call: dict,
        result: Any,
        status: str,
        start: float,
        error: str | None = None,
    ) -> None:
        """写 tool.call 事件到 thread-events.jsonl。

        事件字段：
        - tool_name / source；
        - args_summary：_summarize_args(tool_call["args"])；
        - status：ok / error / circuit_open；
        - duration_ms：int((now - start) * 1000)；
        - error（可选）：脱敏后的错误信息；
        - externalized / externalized_path（可选）：仅当 status == "ok" 且
          result 是 ToolMessage 且带 POIROT_EXTERNALIZED 标记时写入。

        Args:
            runtime: LangGraph 运行时，用于 _emit。
            tool_name: 工具名。
            source: 工具来源（mcp / builtin / sandbox / unknown）。
            tool_call: 工具调用原始结构，用于取 args。
            result: 工具调用结果（用于判断外化标记）。
            status: "ok" / "error" / "circuit_open"。
            start: 调用开始时间戳（time.time()）。
            error: 脱敏后的错误信息，可选。
        """
        event: dict[str, Any] = {
            "tool_name": tool_name,
            "source": source,
            "args_summary": _summarize_args(tool_call.get("args")),
            "status": status,
            "duration_ms": int((time.time() - start) * 1000),
        }
        if error:
            event["error"] = error
        # 外化联动标记
        if status == "ok" and isinstance(result, ToolMessage):
            additional_kwargs = getattr(result, "additional_kwargs", {}) or {}
            if additional_kwargs.get(POIROT_EXTERNALIZED):
                event["externalized"] = True
                event["externalized_path"] = additional_kwargs.get(POIROT_EXTERNALIZED_PATH, "")
        self._emit(runtime, "tool.call", event)