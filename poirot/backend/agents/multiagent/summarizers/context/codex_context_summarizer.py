"""CodexContextSummarizer — 规则提取（代码片段 + 文件路径，零 LLM）。

【整体职责】
为 Codex specialist 生成 context_summary：从父 ThreadState 中提取代码块和文件路径，
拼成一段精简上下文。零 LLM，纯规则提取，控制 token 成本。

【内容摘要】
- _CODE_BLOCK_RE / _MAX_CODE_BLOCKS / _MAX_PATHS / _MAX_SUMMARY_CHARS : 规则常量。
- _field()                      : 从 dict 或对象中统一取字段。
- CodexContextSummarizer        : 输入端转换器主类。
- summarize()                   : 入口，拼装 goal + 代码块 + 文件路径。
- _extract_code_blocks()        : 从 messages 中提取代码块（最多 3 个）。
- _extract_file_paths()         : 从 artifacts 中提取文件路径（最多 10 个）。

【职责边界】
- 只负责：从 ThreadState 规则提取代码块与文件路径，拼成 context_summary 字符串。
- 不负责：结果压缩（ResultSummarizer 负责）、specialist 执行、LLM 调用。
- 不持有运行时状态：纯函数式读取，不写回 state。

【INVARIANT】
- 零 LLM：全部规则提取，不调用任何模型。
- 不传全量 context：只提取 goal + success_criteria + 代码块 + 文件路径。
- 代码块上限 _MAX_CODE_BLOCKS=3：找够即停。
- 文件路径上限 _MAX_PATHS=10：去重后最多 10 条。
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
# 最多提取的代码块数量。
_MAX_CODE_BLOCKS = 3
# 最多提取的文件路径数量。
_MAX_PATHS = 10
# 总摘要的最大字符数，超出截断。
_MAX_SUMMARY_CHARS = 3000


def _field(item: Any, name: str) -> Any:
    """统一字段访问：dict 走 .get，对象走 getattr，缺失返回 None。"""
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


class CodexContextSummarizer:
    """Codex specialist 输入端转换器（规则提取：代码 + 文件路径）。"""

    def summarize(
        self,
        state: ThreadState,
        goal: str,
        success_criteria: str,
        template: Any | None = None,
    ) -> str:
        """规则提取 context_summary：goal + 代码块 + 文件路径。

        结果超过 _MAX_SUMMARY_CHARS 时截断并附 "...(truncated)"。
        template 未使用（L2 预留）。
        """
        parts: list[str] = [f"Goal: {goal}", f"Success criteria: {success_criteria}"]

        code_blocks = self._extract_code_blocks(state)
        if code_blocks:
            parts.append("Relevant code:")
            parts.extend(code_blocks)

        paths = self._extract_file_paths(state)
        if paths:
            parts.append(f"Files: {', '.join(paths)}")

        summary = "\n".join(parts)
        if len(summary) > _MAX_SUMMARY_CHARS:
            summary = summary[:_MAX_SUMMARY_CHARS] + "\n...(truncated)"
        return summary

    def _extract_code_blocks(self, state: ThreadState) -> list[str]:
        """从 messages 中提取代码块（``` ... ```），最多 _MAX_CODE_BLOCKS=3 个。

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

    def _extract_file_paths(self, state: ThreadState) -> list[str]:
        """从 artifacts 中提取文件路径，去重后最多 _MAX_PATHS=10 条。

        遍历 state["artifacts"]，取 path 字段；空值跳过，重复跳过。
        """
        paths: list[str] = []
        for artifact in state.get("artifacts", []):
            path = _field(artifact, "path")
            if path and path not in paths:
                paths.append(str(path))
            if len(paths) >= _MAX_PATHS:
                break
        return paths