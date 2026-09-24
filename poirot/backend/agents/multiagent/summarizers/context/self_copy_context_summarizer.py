"""SelfCopyContextSummarizer — 自复制 subagent 的输入端转换器。

【整体职责】
为 self-copy subagent 生成 context_summary：优先用 LLM 精准提取研究上下文，
无 LLM 时退回规则提取（goal + success_criteria + user_input + 最近 observations）。
只输出精简字符串，不传全量 ThreadState。

【内容摘要】
- _MAX_OBSERVATIONS / _MAX_SUMMARY_CHARS : 规则提取的截断上限。
- _field()                               : 从 dict 或对象中统一取字段。
- SelfCopyContextSummarizer              : 输入端转换器主类。
- summarize()                            : 入口，按有无 LLM 分派。
- _rule_summarize()                      : 无 LLM 时的规则提取。
- _llm_summarize()                       : 有 LLM 时的精准提取（MVP 为占位实现）。

【职责边界】
- 只负责：从 ThreadState 提取并压缩成 context_summary 字符串。
- 不负责：结果压缩（ResultSummarizer 负责）、specialist 执行、LLM 的构造。
- 不持有运行时状态：LLM 引用由构造参数传入，可为 None。

【INVARIANT】
- 不传全量 context：只输出 goal / success_criteria / user_input / 最近 N 条 observation，
  控制 token 成本。
- 双路径：self._llm 非 None 走 LLM 提取；否则走规则提取。
- 规则路径截断：observations 最多取最近 _MAX_OBSERVATIONS 条；
  总长度超过 _MAX_SUMMARY_CHARS 时截断并附 "...(truncated)"。
- 字段访问容错：_field 同时支持 dict 与对象，缺失返回 None。
- template 参数为 L2 扩展预留，当前两条路径均未使用。
"""
from __future__ import annotations

from typing import Any

from poirot.backend.agents.state.types import ThreadState

# 规则提取时最多取最近 N 条 observation。
_MAX_OBSERVATIONS = 5
# 规则提取结果的最大字符数，超出则截断。
_MAX_SUMMARY_CHARS = 3000


def _field(item: Any, name: str) -> Any:
    """统一字段访问：dict 走 .get，对象走 getattr，缺失返回 None。"""
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


class SelfCopyContextSummarizer:
    """Poirot self-copy subagent 输入端转换器（LLM 提取 + 规则 fallback）。"""

    def __init__(self, llm: Any = None) -> None:
        """初始化。llm 为 None 时走规则提取路径。"""
        self._llm = llm

    def summarize(
        self,
        state: ThreadState,
        goal: str,
        success_criteria: str,
        template: Any | None = None,
    ) -> str:
        """按有无 LLM 分派：有则 LLM 提取，无则规则提取。

        template 为 L2 扩展预留，当前未使用。
        """
        if self._llm is not None:
            return self._llm_summarize(state, goal, success_criteria)
        return self._rule_summarize(state, goal, success_criteria)

    def _rule_summarize(
        self,
        state: ThreadState,
        goal: str,
        success_criteria: str,
        template: Any | None = None,
    ) -> str:
        """规则提取：goal + success_criteria + user_input + 最近 N 条 observation。

        结果超过 _MAX_SUMMARY_CHARS 时截断并附 "...(truncated)"。
        template 未使用（L2 预留）。
        """
        parts: list[str] = [f"Goal: {goal}", f"Success criteria: {success_criteria}"]

        user_input = state.get("user_input")
        if user_input:
            parts.append(f"Original request: {user_input}")

        observations = state.get("observations", [])
        recent = observations[-_MAX_OBSERVATIONS:] if observations else []
        for obs in recent:
            content = _field(obs, "content")
            if content:
                parts.append(f"Finding: {content}")

        summary = "\n".join(parts)
        if len(summary) > _MAX_SUMMARY_CHARS:
            summary = summary[:_MAX_SUMMARY_CHARS] + "\n...(truncated)"
        return summary

    def _llm_summarize(
        self,
        state: ThreadState,
        goal: str,
        success_criteria: str,
        template: Any | None = None,
    ) -> str:
        """LLM 提取研究上下文（MVP 为占位实现）。

        构造 prompt 后调 self._llm.invoke，取返回对象的 content 字段。
        template 未使用（L2 预留）。
        """
        user_input = state.get("user_input", "")
        prompt = (
            f"Extract relevant research context for this subtask.\n"
            f"Goal: {goal}\n"
            f"Success criteria: {success_criteria}\n"
            f"Original request: {user_input}\n"
            f"Return a concise context summary."
        )
        result = self._llm.invoke(prompt)
        content = getattr(result, "content", str(result))
        return str(content)