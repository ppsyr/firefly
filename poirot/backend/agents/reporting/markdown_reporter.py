"""Markdown 报告生成器 — 将 thread_state 渲染为最终 Markdown 报告。

【整体职责】
从 thread_state 中提取研究问题与证据，按优先级产出一份 Markdown 文本，
封装为 ReportResult 返回。供 LeaderAgent 在收尾阶段调用。

【内容摘要】
- MarkdownReporter.generate_report : 主入口，三级 fallback 产出报告文本。
- _render_structured              : Tier 2 兜底，把 observations + sources 渲染为结构化 Markdown。
- _last_ai_message                : Tier 3 兜底，从 messages 中倒序取最后一条 AI 消息文本。
- _summary                        : 生成 Summary 段落（observations 优先，其次 AI 回答）。
- _field                          : 通用字段读取，兼容 dict 与对象属性两种访问方式。

【职责边界】
- 只负责：把已有证据渲染成 Markdown 文本，产出 ReportResult。
- 不负责：证据收集（middleware / tool）、报告事件的发送（由 LeaderAgent.run 发）、
  证据充分性判断（reflection / evidence middleware）。
"""
from __future__ import annotations

from typing import Any

from poirot.backend.agents.reporting.result import ReportResult


class MarkdownReporter:
    """Markdown 报告生成器。

    按三级 fallback 优先级产出报告文本，保证在任何 thread_state 形态下
    都能返回一份可用的 Markdown。
    """

    def generate_report(self, thread_state: dict[str, Any], run_context: Any) -> ReportResult:
        """生成最终报告。

        三级 fallback 优先级：
          ① final_report 字段 —— ReportMiddleware 已合成完整报告（最优）。
          ② 渲染 observations / sources —— 结构化兜底，ReportMiddleware 未跑但已有证据。
          ③ _last_ai_message —— 无证据兜底（fast 模式 / 单轮对话），保留旧行为。

        Args:
            thread_state: 线程状态，含 research_question / user_input / observations /
                sources / final_report / messages 等字段。
            run_context: 运行上下文（当前实现未使用，保留接口以兼容调用方）。

        Returns:
            ReportResult: 仅封装报告文本。
        """
        question = (
            thread_state.get("research_question")
            or thread_state.get("user_input")
            or "Research report"
        )
        observations = thread_state.get("observations", [])
        sources = thread_state.get("sources", [])
        final_report_field = thread_state.get("final_report")

        if final_report_field:
            final_report = final_report_field
        elif observations:
            final_report = _render_structured(question, observations, sources)
        else:
            ai_answer = _last_ai_message(thread_state)
            body = ai_answer or "No answer collected."
            final_report = f"# {question}\n\n{body}"

        # F1: report.generated 事件仅由 agent.py（LeaderAgent.run）发出，reporter 只产文本。
        return ReportResult(final_report=final_report)


def _render_structured(question: str, observations: list[Any], sources: list[Any]) -> str:
    """Tier 2 兜底：把 observations + sources 渲染成结构化 Markdown。

    Args:
        question: 研究问题，作为一级标题。
        observations: 观察列表，渲染为 Findings 列表项。
        sources: 来源列表，渲染为 Sources 链接列表。

    Returns:
        str: 含 Summary / Findings / Sources 三段的 Markdown 文本。
    """
    lines = [f"# {question}", "", "## Summary", _summary(observations, ""), "", "## Findings"]
    if observations:
        lines.extend(f"- {_field(obs, 'content')}" for obs in observations)
    else:
        lines.append("- No observations collected.")
    lines += ["", "## Sources"]
    if sources:
        lines.extend(
            f"- [{_field(src, 'title') or _field(src, 'url')}]({_field(src, 'url')})"
            for src in sources
        )
    else:
        lines.append("- No sources.")
    return "\n".join(lines)


def _last_ai_message(thread_state: dict[str, Any]) -> str:
    """Tier 3 兜底：从 messages 中倒序取最后一条 AI 消息的文本。

    Args:
        thread_state: 线程状态，读取 messages 字段。

    Returns:
        str: 最后一条 AI 消息的文本；无则返回空串。
        兼容 content 为 str 或 list（多模态分段）两种形态。
    """
    messages = thread_state.get("messages", [])
    for msg in reversed(messages):
        content = getattr(msg, "content", None)
        type_name = type(msg).__name__
        if content and "AI" in type_name:
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = [
                    item["text"] if isinstance(item, dict) and "text" in item else str(item)
                    for item in content
                    if item
                ]
                return "".join(parts)
    return ""


def _summary(observations: list[Any], ai_answer: str) -> str:
    """生成 Summary 段落：observations 优先，其次 AI 回答，最后给默认占位。

    Args:
        observations: 观察列表。
        ai_answer: AI 回答文本。

    Returns:
        str: Summary 段落文本。
    """
    if observations:
        return _field(observations[0], "content")
    if ai_answer:
        return ai_answer
    return "No evidence collected yet."


def _field(item: Any, name: str) -> Any:
    """通用字段读取：兼容 dict 与对象属性两种访问方式。

    Args:
        item: 待读取对象，可为 dict 或任意对象。
        name: 字段名。

    Returns:
        Any: 字段值；不存在时返回 None。
    """
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)