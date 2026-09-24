"""MetricsView Protocol — L2 读 L1 metrics 接口。

【整体职责】
定义 L2（以及 L3）读取 L1 指标的统一契约：5 个方法覆盖 specialist 聚合指标、
全局聚合指标、失败分类统计、最近失败记录、specialist 列表。
L2/L3 只依赖此 Protocol，不直接耦合 L1 的 MultiAgentMetricsStore 实现类。

【内容摘要】
- SpecialistMetricsSnapshot(TypedDict) : 单 specialist 聚合快照。
- GlobalMetricsSnapshot(TypedDict)     : 全局聚合快照。
- MetricsView(Protocol, runtime_checkable) : 读 L1 指标契约（5 方法）。

【职责边界】
- 只负责：定义读取契约（方法签名 + 语义）。
- 不负责：指标的实际读写（L1 MultiAgentMetricsStore 负责）、
  指标的消费决策（L2 FailureFocuser / L3 RuntimeTracker 负责）。
- 不持有状态：Protocol 无实现，纯接口。

【INVARIANT】
- runtime_checkable Protocol：支持 isinstance 运行时检查。
- L2/L3 只依赖此 Protocol，不 import MultiAgentMetricsStore 实现类。
- 由 L1 MultiAgentMetricsStore 实现此契约（5 个方法）。
- SpecialistMetricsSnapshot 的 avg_cost_usd / avg_latency_seconds
  来自 JOIN specialist_judgments 表计算。
- completion_rate 分母为 0 时返回 0.0（不抛异常）。
- since 参数语义：None 查全量，非 None 查 since 之后的数据。
- sample_size 用于判断可信度（< 20 时 LLM 可判断可信度低）。
- get_specialist_metrics 返回 None 表示 specialist 不存在或无记录。
"""
from __future__ import annotations

from typing import Any, Protocol, TypedDict, runtime_checkable

from poirot.backend.agents.multiagent.evolution.types import FailureCategory, FailureRecord


class SpecialistMetricsSnapshot(TypedDict):
    """单 specialist 聚合指标快照（get_specialist_metrics 返回）。

    字段：
    - total_*: 4 计数器（selections / invoked / completions / fallbacks）。
    - completion_rate: completions / invoked（0 除保护返 0.0）。
    - avg_cost_usd / avg_latency_seconds: JOIN specialist_judgments 表算 avg。
    - sample_size: 统计样本数（< 20 时 LLM 可判断可信度）。
    """

    specialist_name: str
    total_selections: int
    total_invoked: int
    total_completions: int
    total_fallbacks: int
    completion_rate: float
    avg_cost_usd: float
    avg_latency_seconds: float
    sample_size: int


class GlobalMetricsSnapshot(TypedDict):
    """全局聚合指标快照（get_global_metrics 返回）。

    total_calls / total_cost_usd / avg_latency_seconds 跨所有 specialist 聚合。
    """

    total_calls: int
    total_cost_usd: float
    avg_latency_seconds: float
    total_selections: int
    total_completions: int
    total_fallbacks: int


@runtime_checkable
class MetricsView(Protocol):
    """L2 读 L1 指标的统一契约（runtime_checkable）。

    L2 / L3 模块只依赖此 Protocol，不 import MultiAgentMetricsStore 实现类。
    L1 MultiAgentMetricsStore 实现此契约（5 个方法）。
    INVARIANT：L2 不直接读 OrchestrationStore，只通过 MetricsView Protocol。
    """

    def get_specialist_metrics(
        self, name: str, *, since: float | None = None
    ) -> SpecialistMetricsSnapshot | None:
        """单 specialist 聚合指标快照。

        since=None 查全量；since 非 None 查 since 之后的数据。
        返回 None 表示 specialist 不存在或无记录。

        Args:
            name: specialist 名。
            since: 时间窗口起点（None 表示全量）。

        Returns:
            快照，或 None。
        """
        ...

    def get_global_metrics(
        self, *, since: float | None = None
    ) -> GlobalMetricsSnapshot:
        """全局聚合指标快照（跨所有 specialist）。

        Args:
            since: 时间窗口起点（None 表示全量）。

        Returns:
            全局快照。
        """
        ...

    def get_failure_categories(
        self, *, since: float | None = None
    ) -> dict[FailureCategory, int]:
        """失败分类统计（since 后窗口，按 failure_category 计数）。

        FailureFocuser 读此方法判定 dominant_category。

        Args:
            since: 时间窗口起点（None 表示全量）。

        Returns:
            {FailureCategory: 次数} 映射。
        """
        ...

    def get_recent_failures(
        self, *, category: FailureCategory, limit: int = 10
    ) -> list[FailureRecord]:
        """最近 N 条某类失败记录（FailureFocuser 取 top 样本用）。

        Args:
            category: 失败分类。
            limit: 最多返回条数，默认 10。

        Returns:
            FailureRecord 列表。
        """
        ...

    def list_specialists(self) -> list[str]:
        """所有有记录的 specialist name 列表（candidate metadata 生成用）。

        Returns:
            specialist 名列表。
        """
        ...