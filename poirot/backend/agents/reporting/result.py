"""报告结果数据结构 — frozen dataclass。

【整体职责】
定义 MarkdownReporter 的产出结构 ReportResult：一份最终报告文本，
附带可选的产物引用与警告信息，供 Leader 收尾阶段消费。

【内容摘要】
- ReportResult : 报告结果，含报告文本、产物引用、警告。

【职责边界】
- 只负责：定义报告结果的数据结构、字段语义。
- 不负责：报告文本的生成逻辑（MarkdownReporter）、产物管理（Artifact 存储）、
  事件发送（LeaderAgent.run）。

【INVARIANT】
- frozen：构造后不可变，可跨 Agent / 跨线程安全共享。
- final_report 必填：报告文本是核心产物，无默认值。
- artifacts / warnings 默认空元组：用 field(default_factory=tuple) 避免可变默认值陷阱。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from poirot.backend.agents.state.types import Artifact


@dataclass(frozen=True)
class ReportResult:
    """报告结果。

    由 MarkdownReporter.generate_report 构造并返回，封装最终报告文本，
    可选附带产物引用与警告信息。

    Attributes:
        final_report: 最终报告文本（Markdown 格式）。
        artifacts: 报告关联的产物引用，默认空元组。
        warnings: 生成过程中的警告信息，默认空元组。
    """

    final_report: str
    artifacts: tuple[Artifact, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)