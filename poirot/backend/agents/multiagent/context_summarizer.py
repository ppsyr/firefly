"""ContextSummarizer Protocol — specialist 输入端转换器契约。

【整体职责】
定义 specialist 输入端的转换契约：从 Leader 的完整 ThreadState 中选取相关上下文，
压缩成一段 context_summary 字符串，传给 specialist 使用。
与 ResultSummarizer（输出端）配对，一进一出，控制 token 成本与上下文污染。

【内容摘要】
- ContextSummarizer(Protocol) : 输入端转换器契约，仅一个 summarize() 方法。

【职责边界】
- 只负责：定义转换契约（方法签名 + 语义）。
- 不负责：具体提取规则（各 specialist 实现负责）、输出端压缩
  （ResultSummarizer 负责）、specialist 执行（runtime 负责）。
- 不持有状态：Protocol 无实现，纯接口。

【INVARIANT】
- per-specialist 实现：每个 specialist 有专属 ContextSummarizer，提取规则按能力定制。
- 不传全量 ThreadState：只返回精简 context_summary，避免污染 specialist、控制 token 成本。
- 不暴露 specialist 内部：产出的字符串是给 specialist 的输入，不反向暴露其状态。
- 与 ResultSummarizer 配对：输入端压 context_summary，输出端压 SpecialistResult。
- L2 扩展：template 非 None 时按模板（extractors / filters / max_tokens /
  prompt_skeleton）生成；template=None 时走 L1 默认行为，保证向后兼容。
- 实现示例：CodexContextSummarizer / ClaudeCodeContextSummarizer /
  SelfCopyContextSummarizer。
"""
from __future__ import annotations

from typing import Any, Protocol

from poirot.backend.agents.state.types import ThreadState


class ContextSummarizer(Protocol):
    """specialist 输入端转换器契约。

    实现示例：CodexContextSummarizer / ClaudeCodeContextSummarizer /
    SelfCopyContextSummarizer。
    """

    def summarize(
        self,
        state: ThreadState,
        goal: str,
        success_criteria: str,
        template: Any | None = None,
    ) -> str:
        """从 ThreadState 选取相关上下文，返回精简的 context_summary。

        - 不传全量 ThreadState，避免污染 specialist 并控制 token 成本。
        - 按 specialist 能力定制提取规则（codex 提代码 / claude 提 review 标准 /
          self-copy 用 LLM）。
        - template 非 None 时按模板生成；template=None 时走 L1 默认行为，向后兼容。
        """
        ...