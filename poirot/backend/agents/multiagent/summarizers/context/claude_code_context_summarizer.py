"""ClaudeCodeContextSummarizer — 规则提取（代码 + review 标准，零 LLM）。

【整体职责】
为 Claude Code specialist 生成 context_summary：从父 ThreadState 中提取代码块和
review 相关的关键词，拼成一段精简上下文。零 LLM，纯规则提取，控制 token 成本。

【内容摘要】
- _CODE_BLOCK_RE / _REVIEW_KEYWORDS / _MAX_CODE_BLOCKS / _MAX_SUMMARY_CHARS : 规则常量。
- _field()                          : 从 dict 或对象中统一取字段。
- ClaudeCodeContextSummarizer      : 输入端转换器主类。
- summarize()                       : 入口，拼装 goal + 代码块 + review 提示。
- _extract_code_blocks()            : 从 messages 中提取代码块（最多 2 个）。
- _extract_review_hints()           : 从 messages 中提取 review 关键词（最多 5 个）。

【职责边界】
- 只负责：从 ThreadState 规则提取代码块与 review 关键词，拼成 context_summary 字符串。
- 不负责：结果压缩（ResultSummarizer 负责）、specialist 执行、LLM 调用。
- 不持有运行时状态：纯函数式读取，不写回 state。

【INVARIANT】
- 零 LLM：全部规则提取，不调用任何模型。
- 不传全量 context：只提取 goal + success_criteria + 代码块 + review 关键词。
- 代码块上限 _MAX_CODE_BLOCKS=2：找够即停，控制 token。
- review 关键词上限 5：去重后取前 5 个。
- 总长度上限 _MAX_SUMMARY_CHARS=3000：超出截断并附 "...(truncated)"。
- 字段访问容错：_field 同时支持 dict 与对象，缺失返回 None；非字符串 content 跳过。
- template 参数为 L2 扩展预留，当前未使用。
"""
from __future__ import annotations

import re
from typing import Any

from poirot.backend.agents.state.types import ThreadState

# 代码块正则：匹配 ``` ... ```（含跨行）。
_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```")
# review 相关关键词，命中即作为 review focus。
_REVIEW_KEYWORDS = ("review", "check", "verify", "test", "lint", "security", "performance")
# 最多提取的代码块数量。
_MAX_CODE_BLOCKS = 2
# 总摘要的最大字符数，超出截断。
_MAX_SUMMARY_CHARS = 3000


def _field(item: Any, name: str) -> Any:
    """统一字段访问：dict 走 .get，对象走 getattr，缺失返回 None。"""
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


class ClaudeCodeContextSummarizer:
    """Claude Code specialist 输入端转换器（规则提取：代码 + review 标准）。"""

    def summarize(
        self,
        state: ThreadState,
        goal: str,
        success_criteria: str,
        template: Any | None = None,
    ) -> str:
        """规则提取 context_summary：goal + 代码块 + review 提示。

        结果超过 _MAX_SUMMARY_CHARS 时截断并附 "...(truncated)"。
        template 未使用（L2 预留）。
        """
        parts: list[str] = [f"Goal: {goal}", f"Success criteria: {success_criteria}"]

        code_blocks = self._extract_code_blocks(state)
        if code_blocks:
            parts.append("Code to review:")
            parts.extend(code_blocks)

        review_hints = self._extract_review_hints(state)
        if review_hints:
            parts.append(f"Review focus: {', '.join(review_hints)}")

        summary = "\n".join(parts)
        if len(summary) > _MAX_SUMMARY_CHARS:
            summary = summary[:_MAX_SUMMARY_CHARS] + "\n...(truncated)"
        return summary

    def _extract_code_blocks(self, state: ThreadState) -> list[str]:
        """从 messages 中提取代码块（``` ... ```），最多 _MAX_CODE_BLOCKS 个。

        遍历 messages 的 content，仅处理 str 类型；找够即停。
        """
        blocks: list[str] = []
        for msg in state.get("messages", []):
            content = _field(msg, "content")
            if not isinstance(content, str):
                continue
            found = _CODE_BLOCK_RE.findall(content)
            blocks.extend(found)
            if len(blocks) >= _MAX_CODE_BLOCKS:
                break
        return blocks[:_MAX_CODE_BLOCKS]

    def _extract_review_hints(self, state: ThreadState) -> list[str]:
        """从 messages 中提取 review 关键词（去重，最多 5 个）。

        遍历 messages 的 content，小写匹配 _REVIEW_KEYWORDS；非字符串 content 跳过。
        """
        hints: list[str] = []
        for msg in state.get("messages", []):
            content = _field(msg, "content")
            if not isinstance(content, str):
                continue
            lower = content.lower()
            for kw in _REVIEW_KEYWORDS:
                if kw in lower and kw not in hints:
                    hints.append(kw)
        return hints[:5]