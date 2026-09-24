"""ReportMiddleware — ReAct 循环结束后独立合成 final_report。

【整体职责】
在 after_agent 阶段（ReAct 循环结束之后），如果 state 里已有 observations，
就单独调一次 LLM，基于 observations + sources + errors 合成一份结构化
Markdown 报告，写回 state["final_report"]。

【为什么不复用最后一条 AIMessage】
不再赌「最后一条 AIMessage 就是最终答案」（deer-flow 模式），
而是独立发起一次报告合成调用，由专门的 reporter prompt 生成报告。

【启用范围】
仅 general / expert 模式启用。default 模式不自动合成，报告靠 /report 命令触发。

【MVP 说明】
MVP 阶段复用 researcher 模型来当 reporter（D5）。
"""

from __future__ import annotations

from typing import Any, override

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.runtime import Runtime

from poirot.backend.agents.prompts import get_prompt_manager
from poirot.backend.agents.state.types import ThreadState


def get_reporter_system() -> str:
    """加载 reporter 系统 prompt（从 prompts/system/reporter/system.md）。"""
    return get_prompt_manager().load("reporter", "system")


def _format_observations(observations: list[Any]) -> str:
    """把 observations 列表格式化成可读文本。

    处理流程：
    1. 空列表 → "（无观察记录）"。
    2. 逐条输出：
       "[{observation_id}] (step={step_id}, sources={source_refs})\n{content}"。
       - observation_id 缺省 "?"；
       - step_id 缺省 "-"；
       - content 缺省 ""；
       - source_refs 用 ", " 连接，缺省 "无"。
    3. 各条之间用 "\\n\\n" 分隔。

    Args:
        observations: Observation 列表。

    Returns:
        格式化文本。
    """
    if not observations:
        return "（无观察记录）"
    lines: list[str] = []
    for obs in observations:
        oid = _field(obs, "observation_id") or "?"
        sid = _field(obs, "step_id") or "-"
        content = _field(obs, "content") or ""
        refs = _field(obs, "source_refs") or ()
        refs_str = ", ".join(refs) if refs else "无"
        lines.append(f"[{oid}] (step={sid}, sources={refs_str})\n{content}")
    return "\n\n".join(lines)


def _format_sources(sources: list[Any]) -> str:
    """把 sources 列表格式化成可读文本。

    处理流程：
    1. 空列表 → "（无来源）"。
    2. 逐条输出 "[{source_id}] {title} — {url}"（rstrip 掉多余 " —"）。
       - source_id 缺省 "?"；
       - title / url 缺省 ""。
    3. 各条之间用 "\\n" 分隔。

    Args:
        sources: Source 列表。

    Returns:
        格式化文本。
    """
    if not sources:
        return "（无来源）"
    lines: list[str] = []
    for src in sources:
        sid = _field(src, "source_id") or "?"
        url = _field(src, "url") or ""
        title = _field(src, "title") or ""
        lines.append(f"[{sid}] {title} — {url}".rstrip(" —"))
    return "\n".join(lines)


def _format_errors(errors: list[Any]) -> str:
    """把 errors 列表汇总成可读文本（只展示 failure）。

    处理流程：
    1. 过滤 kind == "success" 的条目，只保留 failures。
    2. 无 failures → 返回空串（不生成 errors 区块）。
    3. 首行："以下工具调用失败（共 N 次），对应信息未能获取："。
    4. 按 error_type 汇总为 by_type: {error_type: [(tool_name, reason), ...]}。
    5. 每个 error_type 输出一行：
       "- [{et}] {reason}：涉及工具 {tool_list}"。
       - reason 取该类型下第一个非空值；
       - tool_list 去重并排序。
    6. 用 "\\n" 连接。

    Args:
        errors: AgentError 列表。

    Returns:
        格式化文本；无失败时返回空串。
    """
    failures = [e for e in errors if _field(e, "kind") != "success"]
    if not failures:
        return ""
    lines = [f"以下工具调用失败（共 {len(failures)} 次），对应信息未能获取："]
    # 按 error_type 汇总
    by_type: dict[str, list[tuple[str, str]]] = {}  # et -> [(tool_name, reason)]
    for err in failures:
        et = _field(err, "error_type") or "unknown"
        by_type.setdefault(et, []).append((_field(err, "tool_name") or "unknown", _field(err, "reason") or ""))
    for et, entries in by_type.items():
        tools = sorted({t for t, _ in entries})
        reason = next((r for _, r in entries if r), "")
        lines.append(f"- [{et}] {reason}：涉及工具 {', '.join(tools)}")
    return "\n".join(lines)


def _field(item: Any, name: str) -> Any:
    """统一字段访问：dict 用 get，其他对象用 getattr（无则 None）。"""
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def _build_reporter_messages(state: dict[str, Any]) -> list[Any]:
    """构造 reporter 调用的 messages（SystemMessage + HumanMessage）。

    处理流程：
    1. question = research_question 或 user_input 或 "Research report"。
    2. observations / sources / errors = state 对应字段（缺省空列表）。
    3. user_content 由三段拼接：
       - "# 研究问题\\n{question}"；
       - "# 已收集的观察（observations）\\n{_format_observations(...)}"；
       - "# 来源（sources）\\n{_format_sources(...)}"。
    4. 若 _format_errors(errors) 非空，追加
       "# 工具失败（errors）\\n{err_block}"。
    5. 末尾追加 "请基于以上证据撰写完整研究报告。"。
    6. 返回 [SystemMessage(get_reporter_system()), HumanMessage(user_content)]。

    Args:
        state: 当前 state（dict）。

    Returns:
        reporter 调用的消息列表。
    """
    question = state.get("research_question") or state.get("user_input") or "Research report"
    observations = state.get("observations") or []
    sources = state.get("sources") or []
    errors = state.get("errors") or []

    user_content = (
        f"# 研究问题\n{question}\n\n"
        f"# 已收集的观察（observations）\n{_format_observations(observations)}\n\n"
        f"# 来源（sources）\n{_format_sources(sources)}\n"
    )
    err_block = _format_errors(errors)
    if err_block:
        user_content += f"\n# 工具失败（errors）\n{err_block}\n"
    user_content += "\n\n请基于以上证据撰写完整研究报告。"

    return [SystemMessage(content=get_reporter_system()), HumanMessage(content=user_content)]


class ReportMiddleware(AgentMiddleware):
    """after_agent 阶段独立合成 final_report。MVP 复用 researcher 模型（D5）。

    只在 observations 非空且 auto_synthesize 为真时才合成。
    """

    state_schema = ThreadState  # type: ignore[assignment]

    def __init__(self, model: BaseChatModel, auto_synthesize: bool = True) -> None:
        """初始化。

        Args:
            model:            用于合成报告的 LLM（MVP 复用 researcher 模型）。
            auto_synthesize:  是否自动合成。
                True  → after_agent 自动合成；
                False → default 模式，报告靠 /report 命令触发。
        """
        self._model = model
        self._auto_synthesize = auto_synthesize

    def _synthesize(self, state: dict[str, Any]) -> str:
        """同步合成报告：构造 messages → 在 interrupt_protection 内调 LLM。

        处理流程：
        1. _build_reporter_messages(state) 构造消息。
        2. 在 interrupt_protection() 上下文内调 self._model.invoke
           （tag="internal_llm"）。
        3. 取响应 content（无则 str(response)）返回。

        Args:
            state: 当前 state（dict）。

        Returns:
            报告正文文本。
        """
        from poirot.backend.agents.observability.interrupt_protection import (
            interrupt_protection,
        )
        messages = _build_reporter_messages(state)
        with interrupt_protection():
            response = self._model.invoke(messages, config={"tags": ["internal_llm"]})
        return getattr(response, "content", str(response))

    async def _asynthesize(self, state: dict[str, Any]) -> str:
        """异步合成报告：逻辑与同步版一致，仅 invoke 改为 await ainvoke。

        Args:
            state: 当前 state（dict）。

        Returns:
            报告正文文本。
        """
        from poirot.backend.agents.observability.interrupt_protection import (
            interrupt_protection,
        )
        messages = _build_reporter_messages(state)
        with interrupt_protection():
            response = await self._model.ainvoke(messages, config={"tags": ["internal_llm"]})
        return getattr(response, "content", str(response))

    @override
    def after_agent(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:
        """同步 after_agent：条件满足时合成 final_report。

        处理流程：
        1. 若 auto_synthesize 为 False（default 模式）→ 返回 None。
        2. 取 state.observations；为空 → 返回 None。
        3. 调 _synthesize 合成报告。
        4. 返回 {"final_report": report}。

        Args:
            state:   当前 ThreadState。
            runtime: LangGraph 运行时（本 hook 未使用）。

        Returns:
            含 final_report 的 state patch；无 observations 时返回 None。
        """
        # default 模式（auto_synthesize=False）：不自动合成，报告靠 /report 命令触发。
        if not self._auto_synthesize:
            return None
        observations = state.get("observations") or []  # type: ignore[assignment]
        if not observations:
            return None
        report = self._synthesize(state if isinstance(state, dict) else dict(state))
        return {"final_report": report}

    @override
    async def aafter_agent(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:
        """异步 after_agent：逻辑与同步版一致，仅合成改为 await。

        处理流程：
        1. auto_synthesize 为 False → None。
        2. observations 为空 → None。
        3. await _asynthesize 合成报告。
        4. 返回 {"final_report": report}。

        Args:
            state:   当前 ThreadState。
            runtime: LangGraph 运行时（本 hook 未使用）。

        Returns:
            含 final_report 的 state patch；无 observations 时返回 None。
        """
        if not self._auto_synthesize:
            return None
        observations = state.get("observations") or []  # type: ignore[assignment]
        if not observations:
            return None
        report = await self._asynthesize(state if isinstance(state, dict) else dict(state))
        return {"final_report": report}