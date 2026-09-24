"""SelfCopyResultSummarizer — self-copy subagent 的输出端转换器。

【整体职责】
为 self-copy subagent 提供输出端转换：直接继承 BaseResultSummarizer，
复用其通用校验（programmatic eval floor），不做任何 self-copy 专属扩展。

【内容摘要】
- SelfCopyResultSummarizer : 继承 BaseResultSummarizer，仅传入 specialist_name="subagent"。

【职责边界】
- 只负责：通过继承复用 BaseResultSummarizer 的 summarize 逻辑。
- 不负责：定义新的校验规则、压缩策略（都由基类提供）。
- 不持有状态：仅一个固定的 specialist_name。

【INVARIANT】
- 无特定扩展：本类不覆写任何基类方法，纯继承。
- specialist_name 固定为 "subagent"。
- programmatic eval floor 由基类保证。
"""
from __future__ import annotations

from poirot.backend.agents.multiagent.summarizers.result.base import (
    BaseResultSummarizer,
)


class SelfCopyResultSummarizer(BaseResultSummarizer):
    """self-copy subagent 输出端转换器（纯继承基类，无专属扩展）。"""

    def __init__(self) -> None:
        """初始化，固定 specialist_name="subagent"。"""
        super().__init__(specialist_name="subagent")