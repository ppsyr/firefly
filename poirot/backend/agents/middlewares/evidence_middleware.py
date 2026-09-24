"""EvidenceMiddleware — 拦截证据类工具调用，沉淀 Source / Observation / AgentError 进 ThreadState。

【整体职责】
只在 wrap_tool_call 层拦截一批「证据类工具」的调用，把工具返回的结果
结构化成两类旁路存档：
- Observation：结果的裁剪正文（约 800 字），并关联本次抽取到的 Source；
- Source：从文本里启发式抽取出的 URL。
同时把工具原始结果继续作为 ToolMessage 写回 messages（模型可见），
实现「双写」：messages 给模型看，observations / sources 给旁路存档用。

【设计要点】
- 激活 observations / sources / errors 这几个原本闲置的 state 字段。
- 只挂 wrap_tool_call hook，不改 ReAct 内核（不替模型做决策）。
- 仅处理白名单内的证据类工具；其余工具直接 passthrough。
- 本中间件只负责「成功时抽取证据」；失败分类 / 账本归 ToolCallMiddleware
  （它在外层捕获异常）。

【为什么只在这里做抽取】
证据抽取依赖工具结果文本，属横切逻辑；把它放在中间件里做，
业务工具实现本身不需要感知 observation / source 结构。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, override

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.runtime import Runtime
from langgraph.types import Command

from poirot.backend.agents.state.types import Observation, Source, ThreadState

# 证据类工具白名单（D9：MVP 手维护）。非白名单工具直接 passthrough。
_EVIDENCE_TOOLS: frozenset[str] = frozenset({
    "web_search",
    "deep_search",
    "browse_page",
    "get_page_links",
    "github_search",
    "github_repo_files",
})

_OBS_CONTENT_MAX = 800   # Observation 正文最大字符数
_URL_RE = re.compile(r"https?://[^\s\)\]\}\>\"']+")  # URL 提取正则


def _make_id(prefix: str) -> str:
    """生成带前缀的短 id（前缀 + 12 位 hex）。"""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _now_iso() -> str:
    """当前 UTC 时间，ISO 格式（秒级精度）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _tool_text(tool_msg: ToolMessage) -> str:
    """把 ToolMessage 的 content 归一化成纯文本。

    支持形态：
    - str：原样返回。
    - list：逐项拼接——dict 含 "text" 取其值；str 直接取用；其他忽略。
    - 其他：str(content) 兜底。

    Args:
        tool_msg: 待归一化的 ToolMessage。

    Returns:
        归一化后的纯文本。
    """
    content = tool_msg.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return str(content)


def _extract_sources(tool_name: str, tool_msg: ToolMessage) -> list[Source]:
    """从 ToolMessage 文本中启发式抽取 URL，构造 Source 列表（按 url 去重）。

    处理流程：
    1. _tool_text 把 content 归一化成纯文本。
    2. 用 _URL_RE 扫描所有 URL；每个 URL 去掉末尾的 .,;: 后去重。
    3. 每个唯一 URL 构造一个 Source（source_type="web"，title/summary 留空，
       retrieved_at 取当前 UTC 时间）。

    Args:
        tool_name: 工具名（本函数当前未直接使用，保留形参便于后续扩展）。
        tool_msg:  工具返回的 ToolMessage。

    Returns:
        去重后的 Source 列表。
    """
    text = _tool_text(tool_msg)
    seen: set[str] = set()
    sources: list[Source] = []
    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;:")
        if url in seen:
            continue
        seen.add(url)
        sources.append(Source(
            source_id=_make_id("src"),
            url=url,
            title="",
            source_type="web",
            retrieved_at=_now_iso(),
            summary="",
        ))
    return sources


def _make_observation(
    tool_name: str,
    tool_msg: ToolMessage,
    sources: list[Source],
    step_id: str | None,
) -> Observation:
    """把工具结果裁剪成 Observation，并关联本次抽取的 Source。

    处理流程：
    1. _tool_text 归一化成纯文本，截取前 _OBS_CONTENT_MAX 个字符作为正文。
    2. 构造 Observation：
       - observation_id 用 _make_id("obs")；
       - step_id 关联当前步骤；
       - content 为裁剪后的正文；
       - source_refs 取 sources 的 source_id 元组；
       - created_at 取当前 UTC 时间。

    Args:
        tool_name: 工具名（本函数当前未直接使用，保留形参便于后续扩展）。
        tool_msg:  工具返回的 ToolMessage。
        sources:   本次抽取到的 Source 列表。
        step_id:   关联的步骤 id，可为 None。

    Returns:
        构造好的 Observation。
    """
    text = _tool_text(tool_msg)
    content = text[:_OBS_CONTENT_MAX]
    return Observation(
        observation_id=_make_id("obs"),
        step_id=step_id,
        content=content,
        source_refs=tuple(s.source_id for s in sources),
        created_at=_now_iso(),
    )


def _resolve_step_id(state: Any) -> str | None:
    """取当前步骤 id；无则从 todos 派生兜底（F4），避免 None。

    背景：首轮模型可能直接调搜索（未先 write_todos）→ current_step_id
    缺失 → step_id=None 会导致 Reflection 覆盖度误判。这里给一个兜底，
    把 observation 关联到最近 in_progress 的 todo，或 todo-0。

    处理流程：
    1. state 非 dict → None。
    2. state["current_step_id"] 有值 → 直接返回。
    3. 取 state["todos"]；为空 → None。
    4. 遍历 todos：
       - 遇到首个 status == "in_progress" 的项 → 返回 f"todo-{idx}"；
       - 遍历完无 in_progress → 返回 "todo-0"。

    Args:
        state: 当前 state。

    Returns:
        步骤 id；无法确定时返回 None。
    """
    if not isinstance(state, dict):
        return None
    step_id = state.get("current_step_id")
    if step_id:
        return step_id
    todos = state.get("todos") or []
    if not todos:
        return None
    # 优先首个 in_progress todo 的 index
    for idx, t in enumerate(todos):
        if isinstance(t, dict) and t.get("status") == "in_progress":
            return f"todo-{idx}"
    # 无 in_progress 则兜底 todo-0
    return "todo-0"


class EvidenceMiddleware(AgentMiddleware):
    """拦截证据类工具调用，把结果结构化沉淀进 observations / sources / errors。

    非证据工具直接 passthrough。证据工具返回 Command(update=...) 双写：
    messages 保留 ToolMessage（模型可见），observations / sources 旁路存档。
    """

    state_schema = ThreadState  # type: ignore[assignment]

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        """同步 wrap_tool_call：白名单过滤 → handler → 抽取 → 双写返回。

        处理流程：
        1. 取 tool_name；不在 _EVIDENCE_TOOLS 白名单 → handler(request) 透传。
        2. 调 handler(request) 取 result。
        3. 若 result 不是 ToolMessage（例如内层返回 Command）→ 原样返回，
           不做抽取。
        4. 在 interrupt_protection() 上下文内做抽取：
           - sources = _extract_sources(...)；
           - step_id = _resolve_step_id(state)；
           - obs     = _make_observation(...)。
        5. 返回 Command(update={observations, sources, messages})，
           其中 messages 里保留原始 ToolMessage 以保证模型可见 + 配对完整。

        说明：本中间件不做失败分类 / 账本；异常与失败归 ToolCallMiddleware
        （它在更外层捕获）。

        Args:
            request: 工具调用请求。
            handler: 下游处理函数。

        Returns:
            证据工具 → Command（含 observations / sources / messages）；
            非证据工具或非 ToolMessage 结果 → handler 原样返回。
        """
        tool_name = request.tool_call.get("name", "")
        if tool_name not in _EVIDENCE_TOOLS:
            return handler(request)

        # FD17：Evidence 只管证据抽取（成功时）；失败分类/账本归 ToolCallMiddleware（外层捕获异常）
        result = handler(request)
        if not isinstance(result, ToolMessage):
            return result

        from poirot.backend.agents.observability.interrupt_protection import (
            interrupt_protection,
        )
        with interrupt_protection():
            sources = _extract_sources(tool_name, result)
            state = request.state
            step_id = _resolve_step_id(state)
            obs = _make_observation(tool_name, result, sources, step_id)
        return Command(update={
            "observations": [obs],
            "sources": sources,
            "messages": [result],
        })

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        """异步 wrap_tool_call：逻辑与同步版一致，仅 handler 改为 await。

        处理流程：
        1. 非白名单工具 → await handler(request) 透传。
        2. result = await handler(request)。
        3. 若 result 不是 ToolMessage → 原样返回。
        4. 在 interrupt_protection() 上下文内做 sources / step_id / obs 抽取。
        5. 返回 Command(update={observations, sources, messages})。

        Args:
            request: 工具调用请求。
            handler: 下游异步处理函数。

        Returns:
            证据工具 → Command（含 observations / sources / messages）；
            非证据工具或非 ToolMessage 结果 → handler 原样返回。
        """
        tool_name = request.tool_call.get("name", "")
        if tool_name not in _EVIDENCE_TOOLS:
            return await handler(request)

        result = await handler(request)
        if not isinstance(result, ToolMessage):
            return result

        from poirot.backend.agents.observability.interrupt_protection import (
            interrupt_protection,
        )
        with interrupt_protection():
            sources = _extract_sources(tool_name, result)
            state = request.state
            step_id = _resolve_step_id(state)
            obs = _make_observation(tool_name, result, sources, step_id)
        return Command(update={
            "observations": [obs],
            "sources": sources,
            "messages": [result],
        })