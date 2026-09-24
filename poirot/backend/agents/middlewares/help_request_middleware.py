"""HelpRequestMiddleware — 拦截 ask_help 工具调用并暂停 graph。

【整体职责】
当 LLM 调用 ask_help 工具时，本中间件在工具真正执行前拦截它：
1. 拦截这次调用，不让它走到实际工具；
2. 把参数格式化成一条对用户友好的求助消息；
3. 返回 Command(goto=END) 暂停 graph，把控制权交回用户；
4. 向 journal 写一条 help.requested 事件。

【模式来源】
借鉴 deer-flow ClarificationMiddleware 的「拦截工具调用 → 暂停 graph」
模式。

【触发条件】
只看 tool_call["name"] == "ask_help"；其他工具一律透传给下游 handler。
"""

from __future__ import annotations

from typing import Any, override

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.graph import END
from langgraph.types import Command

from poirot.backend.agents.middlewares.run_journal_middleware import _get_runtime_value

# help_type → 展示图标（未命中时用默认 "❓"）
_HELP_TYPE_ICONS = {
    "missing_info": "❓",
    "approach_choice": "🔀",
    "risk_confirmation": "⚠️",
    "stuck_report": "🚧",
}


def _format_help_message(args: dict[str, Any]) -> str:
    """把 ask_help 的参数格式化成给用户看的求助文本。

    处理流程：
    1. 从 args 取 question / help_type / context / options；
       help_type 缺省为 "missing_info"。
    2. 按 help_type 取图标（未命中用 "❓"）。
    3. 首部：
       - 有 context → "{icon} {context}\\n{question}"；
       - 无 context → "{icon} {question}"。
    4. 若 options 非空：空行 + 逐条 "  {i}. {opt}"（从 1 编号）。
    5. 用 "\\n" 连接所有部分。

    Args:
        args: ask_help 工具调用的入参，可含 question / help_type /
              context / options。

    Returns:
        格式化后的求助文本。
    """
    question = args.get("question", "")
    help_type = args.get("help_type", "missing_info")
    context = args.get("context")
    options = args.get("options") or []
    icon = _HELP_TYPE_ICONS.get(help_type, "❓")
    parts = [f"{icon} {context}\n{question}"] if context else [f"{icon} {question}"]
    if options:
        parts.append("")
        for i, opt in enumerate(options, 1):
            parts.append(f"  {i}. {opt}")
    return "\n".join(parts)


class HelpRequestMiddleware(AgentMiddleware):
    """拦截 ask_help 工具调用 → 格式化 → 暂停 graph。

    只处理 ask_help 一个工具；其他工具调用直接透传给下游 handler。
    同步 / 异步路径共用同一个 _handle_help 实现。
    """

    @override
    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        """同步 wrap_tool_call：识别 ask_help 并拦截，其余透传。

        处理流程：
        1. 取 request.tool_call（无则空 dict）。
        2. 若 tool_call["name"] != "ask_help"，直接 handler(request) 透传。
        3. 否则交给 _handle_help 处理（格式化 + 写 journal + 暂停 graph）。

        Args:
            request: 工具调用请求。
            handler: 下游处理函数。

        Returns:
            ask_help 时返回 Command(goto=END)；其他工具返回 handler 结果。
        """
        tool_call = getattr(request, "tool_call", None) or {}
        if tool_call.get("name") != "ask_help":
            return handler(request)
        return self._handle_help(request, tool_call)

    @override
    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        """异步 wrap_tool_call：逻辑与同步版一致，仅透传改为 await。

        处理流程：
        1. 取 request.tool_call（无则空 dict）。
        2. 若 tool_call["name"] != "ask_help"，await handler(request) 透传。
        3. 否则交给 _handle_help 处理。

        Args:
            request: 工具调用请求。
            handler: 下游异步处理函数。

        Returns:
            ask_help 时返回 Command(goto=END)；其他工具返回 handler 结果。
        """
        tool_call = getattr(request, "tool_call", None) or {}
        if tool_call.get("name") != "ask_help":
            return await handler(request)
        return self._handle_help(request, tool_call)

    def _handle_help(self, request: Any, tool_call: dict[str, Any]) -> Command:
        """处理 ask_help：格式化消息 + 写 journal + 返回 Command 暂停 graph。

        处理流程：
        1. 从 tool_call 取 args 与 tool_call_id。
        2. 调 _format_help_message(args) 得到用户可读的求助文本。
        3. 从 request.runtime 取 journal / run_id（runtime 缺失时都取 None）。
        4. 若 journal 非 None：append("help.requested",
           {run_id, help_type, question})。
        5. 返回 Command：
           - update.messages = [ToolMessage(content=message,
                                            tool_call_id=...,
                                            name="ask_help")]；
           - goto = END（暂停 graph，把控制权交回用户）。

        注意：本方法不调用 handler，即 ask_help 的实际工具实现不会被执行。

        Args:
            request:   工具调用请求，用于取 runtime。
            tool_call: 已确认 name == "ask_help" 的工具调用结构。

        Returns:
            带 messages 更新且 goto=END 的 Command。
        """
        args = tool_call.get("args", {})
        tool_call_id = tool_call.get("id", "")
        message = _format_help_message(args)

        runtime = getattr(request, "runtime", None)
        journal = _get_runtime_value(runtime, "journal", None) if runtime else None
        run_id = _get_runtime_value(runtime, "run_id", None) if runtime else None
        if journal is not None:
            journal.append("help.requested", {
                "run_id": run_id,
                "help_type": args.get("help_type", "missing_info"),
                "question": args.get("question", ""),
            })

        return Command(
            update={"messages": [ToolMessage(
                content=message, tool_call_id=tool_call_id, name="ask_help",
            )]},
            goto=END,
        )