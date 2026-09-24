"""SummarizerExecutor — P4 全量摘要执行器。

【整体职责】
P4（fraction >= 0.80）时，把旧消息交给 LLM 压成一条 summary HumanMessage，
并做 pairing 保护：
    - 切分点不能落在 ToolMessage 上（_snap_to_pairing）；
    - preserved 段的孤立 ToolMessage / 孤立 AIMessage(tool_calls) 要移到 to_summarize；
    - to_summarize 段里的孤立 ToolMessage 先外化（_externalize_orphans），路径写进摘要。

【与 strategy.py / externalizer.py 的关系】
    strategy.before_model 的 P4 分支：snapshot → summarize_if_pending(..., externalizer)
    summarizer 内部复用 externalizer.externalize_if_needed 处理孤立 ToolMessage。

【产出 messages_patch】
    [RemoveMessage(id=REMOVE_ALL_MESSAGES), summary_msg, *preserved]
    即：清空全部消息，再插入 summary + 保留的近期消息。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from poirot.backend.agents.context_engineering.contract import GovernanceResult
from poirot.backend.agents.context_engineering.strategies.default._constants import CST
from poirot.backend.agents.middlewares.tagged_context_middleware import (
    POIROT_EXTERNALIZED_PATH,
    POIROT_SUMMARY,
)

if TYPE_CHECKING:
    from poirot.backend.agents.context_engineering.strategies.default.externalizer import ExternalizerExecutor


class SummarizerExecutor:
    """P4 全量 summarize + pairing 保护。"""

    def __init__(self, model: Any = None, preserve_recent: int = 6) -> None:
        """构造摘要器。

        Args:
            model:           摘要用模型（strategy 传入 summarize_model 或 model）。
            preserve_recent: 保留最近 N 条消息不摘要。
        """
        self._model = model
        self._preserve_recent = preserve_recent

    def summarize_if_pending(self, governance: dict | None, messages: list, externalizer: ExternalizerExecutor) -> GovernanceResult | None:
        """若 pending 含 P4，则执行全量摘要，返回 GovernanceResult。

        Args:
            governance:   现有 governance。
            messages:     当前消息列表。
            externalizer: ExternalizerExecutor，用于外化孤立 ToolMessage。

        Returns:
            GovernanceResult（state_patch=summary 相关，messages_patch=[RemoveAll, summary, *preserved]）；
            非 P4 / 无模型 / 无可摘要内容时返回 None。

        流程：
            1. _partition 切分 to_summarize / preserved（含 pairing 校正）。
            2. _externalize_orphans 把 to_summarize 里的孤立 ToolMessage 外化，收集路径。
            3. _call_llm 生成摘要文本（失败 fallback "压缩失败，保留最近对话。"）。
            4. 若有外化路径，追加到摘要文本末尾。
            5. 构造 summary HumanMessage + RemoveAll + preserved 作为 messages_patch。
        """
        governance = governance or {}
        pending = (governance.get("default") or {}).get("pending") or []
        if "P4" not in pending:
            return None
        if not self._model:
            return None
        to_summarize, preserved = self._partition(messages)
        if not to_summarize:
            return None
        externalized_paths = self._externalize_orphans(to_summarize, externalizer)
        summary_text = self._call_llm(to_summarize)
        if summary_text is None:
            summary_text = "压缩失败，保留最近对话。"
        if externalized_paths:
            summary_text += "\n\n外化工具结果：" + ", ".join(externalized_paths)
        summary_msg = HumanMessage(content=summary_text, additional_kwargs={POIROT_SUMMARY: True})
        return GovernanceResult(
            state_patch={"governance": self._update_summary(governance, summary_text)},
            messages_patch=[RemoveMessage(id=REMOVE_ALL_MESSAGES), summary_msg, *preserved],
        )

    def _partition(self, messages: list) -> tuple[list, list]:
        """切分 to_summarize / preserved，并做 pairing 校正。

        Args:
            messages: 当前消息列表。

        Returns:
            (to_summarize, preserved)。

        规则：
            - 若消息数 <= preserve_recent，返回 ([], messages)。
            - 否则 cut = n - preserve_recent，再 _snap_to_pairing 校正。
            - preserved 段孤立 tool / ai 消息由 _strip_orphan_tools 移到 to_summarize。
        """
        n = len(messages)
        if n <= self._preserve_recent:
            return [], messages
        cut = n - self._preserve_recent
        cut = self._snap_to_pairing(messages, cut)
        to_summarize = list(messages[:cut])
        preserved = list(messages[cut:])
        # preserved 孤立 ToolMessage（配对 AIMessage 在 to_summarize）移到 to_summarize，防 400
        preserved, orphans = self._strip_orphan_tools(preserved)
        to_summarize.extend(orphans)
        return to_summarize, preserved

    @staticmethod
    def _strip_orphan_tools(preserved: list) -> tuple[list, list]:
        """preserved 中孤立 ToolMessage 或孤立 AIMessage(tool_calls) 移除，防 pairing 断裂。

        Args:
            preserved: 保留段消息列表。

        Returns:
            (clean, orphans)：
                clean   = pairing 完好的消息；
                orphans = 被移出的孤立消息（交给 to_summarize）。
        """
        ai_tc_ids: set[str] = set()
        for msg in preserved:
            if isinstance(msg, AIMessage):
                for tc in msg.tool_calls or []:
                    tc_id = tc.get("id") if isinstance(tc, dict) else None
                    if tc_id:
                        ai_tc_ids.add(tc_id)
        tool_ids: set[str] = set()
        for msg in preserved:
            if isinstance(msg, ToolMessage):
                tool_ids.add(msg.tool_call_id)
        clean: list = []
        orphans: list = []
        for msg in preserved:
            is_orphan_tool = isinstance(msg, ToolMessage) and msg.tool_call_id not in ai_tc_ids
            is_orphan_ai = isinstance(msg, AIMessage) and bool(msg.tool_calls) and not all(
                (tc.get("id") if isinstance(tc, dict) else None) in tool_ids
                for tc in msg.tool_calls
            )
            if is_orphan_tool or is_orphan_ai:
                orphans.append(msg)
            else:
                clean.append(msg)
        return clean, orphans

    def _snap_to_pairing(self, messages: list, cut: int) -> int:
        """把切分点向前对齐到 pairing 完整的位置。

        若 messages[cut] 是 ToolMessage，且 messages[cut-1] 是带 tool_calls 的 AIMessage，
        则 cut 前移 1，保证 AIMessage 与其 ToolMessage 不被切开。

        Args:
            messages: 消息列表。
            cut:      初始切分点。

        Returns:
            校正后的切分点。
        """
        while cut < len(messages) and isinstance(messages[cut], ToolMessage):
            if cut > 0 and isinstance(messages[cut - 1], AIMessage) and messages[cut - 1].tool_calls:
                cut -= 1
            else:
                break
        return cut

    def _externalize_orphans(self, messages: list, externalizer: ExternalizerExecutor) -> list[str]:
        """把 to_summarize 里孤立 ToolMessage 外化，返回路径列表。

        Args:
            messages:     to_summarize 段。
            externalizer: ExternalizerExecutor。

        Returns:
            外化文件路径列表（可能为空）。
        """
        ai_tc_ids: set[str] = set()
        for msg in messages:
            if isinstance(msg, AIMessage):
                for tc in msg.tool_calls or []:
                    tc_id = tc.get("id") if isinstance(tc, dict) else None
                    if tc_id:
                        ai_tc_ids.add(tc_id)
        paths: list[str] = []
        for msg in messages:
            if isinstance(msg, ToolMessage) and msg.tool_call_id not in ai_tc_ids:
                rewritten = externalizer.externalize_if_needed(msg)
                if rewritten is not None:
                    path = rewritten.additional_kwargs.get(POIROT_EXTERNALIZED_PATH)
                    if path:
                        paths.append(path)
        return paths

    def _call_llm(self, messages: list) -> str | None:
        """调 LLM 生成摘要文本。

        Args:
            messages: to_summarize 段。

        Returns:
            摘要文本；模型缺失或调用异常返回 None。

        细节：
            - 通过 prompts 管理器加载 "context_engineering/default/summarize" 提示词，
              把历史拼进 messages_text。
            - config.tags=["internal_llm"] 标记内部调用，
              防 astream(stream_mode="messages") 捕获后泄漏到 CLI。
        """
        if not self._model:
            return None
        try:
            from poirot.backend.agents.prompts import get_prompt_manager

            history = self._format_history(messages)
            prompt = get_prompt_manager().load("context_engineering/default", "summarize", messages_text=history)
            # config tags 标记内部调用，防 astream(stream_mode=messages) 捕获泄漏到 CLI
            response = self._model.invoke(prompt, config={"tags": ["internal_llm"]})
            return response.text.strip() if hasattr(response, "text") else str(response)
        except Exception:
            return None

    @staticmethod
    def _format_history(messages: list) -> str:
        """把消息列表格式化成 "[类型] 内容前 500 字" 的文本块。

        Args:
            messages: 消息列表。

        Returns:
            多行字符串，每行一条消息。
        """
        lines: list[str] = []
        for msg in messages:
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            lines.append(f"[{type(msg).__name__}] {content[:500]}")
        return "\n".join(lines)

    def _update_summary(self, governance: dict, summary_text: str) -> dict:
        """把 summary 文本与 id 写回 governance，并累加 summarize_count。

        Args:
            governance:   现有 governance。
            summary_text: 摘要文本。

        Returns:
            更新后的 governance（default.summary / summary_id / metrics.summarize_count）。
        """
        g = dict(governance or {})
        d = dict(g.get("default") or {})
        d["summary"] = summary_text
        d["summary_id"] = "summary_" + datetime.now(CST).strftime("%H%M%S%f")
        metrics = dict(d.get("metrics") or {})
        metrics["summarize_count"] = metrics.get("summarize_count", 0) + 1
        d["metrics"] = metrics
        g["default"] = d
        return g