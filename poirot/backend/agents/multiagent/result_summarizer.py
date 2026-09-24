"""ResultSummarizer Protocol — specialist 输出端转换器 + programmatic eval 契约。

【整体职责】
定义 specialist 输出端的转换契约：把 runtime 返回的原始输出压缩为 SpecialistResult，
并在此过程中完成 programmatic eval（校验 success_criteria、生成 gap_analysis）。
与 ContextSummarizer（输入端）配对，一进一出，控制 Leader 上下文膨胀。

【内容摘要】
- ResultSummarizer(Protocol) : 输出端转换器契约，仅一个 summarize() 方法。

【职责边界】
- 只负责：定义转换契约（方法签名 + 语义）。
- 不负责：具体压缩策略（各 specialist 的实现负责）、上下文输入压缩
  （ContextSummarizer 负责）、specialist 执行（runtime 负责）。
- 不持有状态：Protocol 无实现，纯接口。

【INVARIANT】
- per-specialist 实现：每个 specialist 有专属 ResultSummarizer，不共用通用逻辑。
- 不回传 raw output 全量：summarize 输出的是摘要，避免 Lead 上下文膨胀。
- success=False 时 gap_analysis 必填：programmatic eval floor。
- 与 ContextSummarizer 配对：输入端压 context_summary，输出端压 SpecialistResult。
- L2 扩展：输出 SpecialistResult.failure_category，供 L2 FailureFocuser 读取。
- 实现示例：BaseResultSummarizer（通用校验）+
  CodexResultSummarizer / ClaudeCodeResultSummarizer / SelfCopyResultSummarizer。
"""
from __future__ import annotations

from typing import Protocol

from poirot.backend.agents.multiagent.types import (
    ArtifactRef,
    SpecialistResult,
)


class ResultSummarizer(Protocol):
    """specialist 输出端转换器 + programmatic eval 契约。

    实现示例：BaseResultSummarizer（通用校验）+ CodexResultSummarizer /
    ClaudeCodeResultSummarizer / SelfCopyResultSummarizer。
    """

    def summarize(
        self,
        raw_output: str,
        artifacts: list[ArtifactRef],
        goal: str,
        success_criteria: str,
    ) -> SpecialistResult:
        """压缩 raw output + 校验 success_criteria，返回 SpecialistResult。

        programmatic eval floor：
        - 校验产物存在性、success_criteria 回应、无敏感改动。
        - success=False 时 gap_analysis 必填。
        - L2 扩展：基于 success + raw_output 启发式生成 failure_category。
        """
        ...