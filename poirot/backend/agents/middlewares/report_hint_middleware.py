"""ReportHintMiddleware —— 把「该检索历史报告了」这件事明确告诉模型。

【为什么要它】
`search_reports` 工具说明里的触发词只是提示：模型仍会因为「答案可能在当前
对话里」「不要每轮都探测」而跳过调用，实测 `之前讨论过` / `之前 report 过`
这类问法经常一次都不调用。本 middleware 用一个确定性的规则判定器检查本轮
最后一条用户消息，命中时通过 `wrap_model_call` 在 system prompt 后追加一段
短提示，让模型在回答前先检索一次。

【为什么不是 intent engine】
`IntentTree.detect_and_dispatch()` 命中即返回 True 并**跳过整个 graph**，它
服务于 `/report`、`/search` 这类一次性命令，不适合「让模型多看一份资料再
回答」；而且它只在 CLI 交互循环里跑，TUI 与 one-shot `run` 都不经过它。
这里是纯本地的规则判断 + 提示注入，没有额外 LLM 调用，也没有额外 graph 节点。

【触发条件（全部满足才提示）】
1. 本轮工具表里有 `search_reports`；
2. 最后一条消息是 HumanMessage（本轮第一次模型调用，循环后续轮不重复提示）；
3. 用户消息命中历史/报告触发词；
4. 本轮还没有调用过 `search_reports`，且调用预算未耗尽；
5. 当前 run 的检索范围可用（有 storage_root 且 thread 绑定了 project+cwd），
   否则提示只会换来一次必然失败的调用。

【INVARIANT】
- 只读：改的是即将发出的 model request，不是 state；消息历史与 checkpoint
  不受影响，提示不会在后续对话里累积。
- 提示只在 system prompt 后追加，不替换原有内容。
- 判定失败（拿不到 tools / runtime / scope）时静默跳过，不影响对话。
"""

from __future__ import annotations

from typing import Any, override

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langgraph.runtime import Runtime

from poirot.backend.agents.middlewares.run_journal_middleware import _get_runtime_value
from poirot.backend.agents.reporting.agent_search import (
    MAX_CALLS_PER_TURN,
    RunSearchScope,
    scope_from_payload,
)

TOOL_NAME = "search_reports"

# 明确提到「报告」：用户既然点了这个产物，就先查再答（用户明确要求的行为）。
_REPORT_WORDS = ("报告", "report", "复盘", "retrospective")

# 指向过去工作的说法：不需要出现「报告」二字也算。
_HISTORY_MARKERS = (
    "之前", "以前", "先前", "上次", "上回", "历史", "原来", "此前", "早前", "过往", "当初",
    "earlier", "previously", "previous", "last time", "in the past", "historical", "we discussed",
)

_HINT = (
    "[report retrieval] The user's latest message refers to earlier reports, past "
    "decisions or previously discussed work. Call `search_reports` once now — query = "
    'the concrete topic they asked about, scope="current", depth="summary" — and base '
    "the answer on what it returns. The saved reports are the only reliable source for "
    "that history; do not answer from memory. Skip the call only if the content the "
    "user asks about is literally present in the messages above."
)


def looks_like_history_question(text: str) -> bool:
    """Rule-based, zero-cost check for "the user is asking about past work".

    Deliberately broad: a false positive costs at most one bounded tool call,
    while a false negative silently drops real history (the bug this fixes).
    """
    if not isinstance(text, str) or not text.strip():
        return False
    folded = text.casefold()
    return any(word in folded for word in _REPORT_WORDS + _HISTORY_MARKERS)


def retrieval_available(scope: RunSearchScope | None) -> bool:
    """Whether a ``scope="current"`` search could actually return something."""
    return bool(scope and scope.storage_root and scope.project and scope.cwd)


class ReportHintMiddleware(AgentMiddleware):
    """``wrap_model_call`` 注入历史检索提示，不改变 state 与消息历史。"""

    name = "ReportHintMiddleware"

    def _hint(self, request: ModelRequest) -> str | None:
        try:
            tools = getattr(request, "tools", None) or []
            names = {getattr(tool, "name", None) or getattr(tool, "__name__", "") for tool in tools}
            if TOOL_NAME not in names:
                return None
            messages = list(getattr(request, "messages", None) or [])
            if not messages or not isinstance(messages[-1], HumanMessage):
                return None
            used = sum(1 for message in messages
                       if isinstance(message, ToolMessage) and getattr(message, "name", None) == TOOL_NAME)
            if used >= MAX_CALLS_PER_TURN:
                return None
            if not looks_like_history_question(str(messages[-1].content)):
                return None
            runtime: Runtime | Any = getattr(request, "runtime", None)
            scope = scope_from_payload(_get_runtime_value(runtime, "report_search_scope"))
            if not retrieval_available(scope):
                return None
        except Exception:
            return None
        return _HINT

    @staticmethod
    def _merge(base: Any, hint: str) -> SystemMessage:
        content = getattr(base, "content", "") if base is not None else ""
        return SystemMessage(content=f"{content}\n\n{hint}" if content else hint,
                             id=getattr(base, "id", None))

    @override
    def wrap_model_call(self, request: ModelRequest, handler) -> Any:
        hint = self._hint(request)
        if hint is None:
            return handler(request)
        return handler(request.override(system_message=self._merge(request.system_message, hint)))

    @override
    async def awrap_model_call(self, request: ModelRequest, handler) -> Any:
        hint = self._hint(request)
        if hint is None:
            return await handler(request)
        return await handler(request.override(system_message=self._merge(request.system_message, hint)))
