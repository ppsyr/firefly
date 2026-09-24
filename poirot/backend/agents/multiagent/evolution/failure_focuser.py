"""FailureFocuser — 失败聚焦（不调 LLM）。

【整体职责】
读 L1 ResultSummarizer 已分类的 failure_category，在 24h 窗口内统计各类失败次数，
按 failure_category 聚类取 top 样本（每类 top 2，总上限 5），产出 FailureStats
喂给 EvolutionMutator。全程不调 LLM——分类已在 L1 完成。

【内容摘要】
- _NON_EVOLVABLE_CATEGORIES         : 不可演化类集合（GOAL_UNCLEAR / SANDBOX_ISSUE）。
- select_failure_samples()          : 过滤不可演化类 + 聚类取 top 样本。
- FailureFocuser                    : 失败聚焦主类（analyze）。

【职责边界】
- 只负责：读 L1 失败分类、聚类取样本、判定 dominant_category。
- 不负责：失败分类（L1 ResultSummarizer 负责）、演化变异（mutator 负责）、
  LLM 调用（本类不调 LLM）。
- 不持有运行时状态：window_seconds 由构造注入。

【INVARIANT】
- 不调 LLM：分类在 L1 ResultSummarizer 已完成。
- 不可演化类（GOAL_UNCLEAR / SANDBOX_ISSUE）过滤：不进入样本、不作 dominant。
- 聚类规则：每类按 severity 降序取 top 2；总上限 5。
- dominant_category：占比最高的可演化类别；无可演化类时为 None。
- 窗口默认 24h（86400s），可配置。
- by_category 保留全部分类计数（含不可演化类，供观察）。
- sample_failures 只含可演化类样本。
"""
from __future__ import annotations

from poirot.backend.agents.multiagent.evolution.metrics_view import MetricsView
from poirot.backend.agents.multiagent.evolution.types import (
    FailureCategory,
    FailureRecord,
    FailureStats,
)

# 不可演化类（不进入 L2 演化流程，转告警）。
_NON_EVOLVABLE_CATEGORIES = frozenset({
    FailureCategory.GOAL_UNCLEAR,
    FailureCategory.SANDBOX_ISSUE,
})


def select_failure_samples(
    failures: list[FailureRecord],
    max_per_category: int = 2,
    max_total: int = 5,
) -> list[FailureRecord]:
    """过滤不可演化类 + 按 failure_category 聚类取 top。

    - 每类取 severity top max_per_category 个。
    - 总上限 max_total。
    - GOAL_UNCLEAR / SANDBOX_ISSUE 过滤掉（不进入样本）。

    Args:
        failures: 候选失败记录列表。
        max_per_category: 每类最多取几个，默认 2。
        max_total: 总样本上限，默认 5。

    Returns:
        聚类取 top 后的样本列表。
    """
    # 过滤不可演化类
    evolvable = [f for f in failures if f.failure_category not in _NON_EVOLVABLE_CATEGORIES]
    # 按 failure_category 聚类
    by_cat: dict[FailureCategory, list[FailureRecord]] = {}
    for f in evolvable:
        by_cat.setdefault(f.failure_category, []).append(f)
    # 每类按 severity 降序取 top max_per_category
    samples: list[FailureRecord] = []
    for cat in sorted(by_cat.keys(), key=lambda c: c.value):
        cat_records = sorted(by_cat[cat], key=lambda r: r.severity, reverse=True)
        samples.extend(cat_records[:max_per_category])
    # 总上限 max_total
    return samples[:max_total]


class FailureFocuser:
    """失败聚焦。

    analyze 读 L1 ResultSummarizer 输出的 failure_category，分类统计，
    喂给 EvolutionMutator。不调 LLM（分类已在 L1 完成）。
    """

    def __init__(self, window_seconds: float = 86400.0) -> None:
        """初始化。窗口默认 24h。

        Args:
            window_seconds: 统计窗口（秒），默认 86400（24h）。
        """
        self._window_seconds = window_seconds

    def analyze(
        self,
        metrics_view: MetricsView,
        profile: str = "default",
    ) -> FailureStats:
        """读 L1 failure_category，分类统计窗口内数据，聚类取 top 样本。

        流程：
        1. 计算 since = now - window_seconds。
        2. get_failure_categories(since) → 各分类计数。
        3. 对可演化类，逐个 get_recent_failures(category, limit=10)。
        4. select_failure_samples 聚类取 top（每类 top 2，总上限 5）。
        5. dominant_category = 占比最高的可演化类别（无则 None）。
        6. 构造 FailureStats。

        Args:
            metrics_view: L2 MetricsView Protocol（读 L1 指标）。
            profile: 演化 profile（MVP 未使用，预留）。

        Returns:
            FailureStats（by_category / dominant_category / sample_failures）。
        """
        import time
        since = time.time() - self._window_seconds
        cats = metrics_view.get_failure_categories(since=since)

        # 按类别取最近失败记录（用于 sample_failures）
        all_failures: list[FailureRecord] = []
        for cat in cats:
            if cat in _NON_EVOLVABLE_CATEGORIES:
                continue  # 不可演化类不取样本
            records = metrics_view.get_recent_failures(category=cat, limit=10)
            all_failures.extend(records)

        # select_failure_samples 聚类取 top
        samples_list = select_failure_samples(all_failures)
        sample_failures: dict[FailureCategory, list[FailureRecord]] = {}
        for s in samples_list:
            sample_failures.setdefault(s.failure_category, []).append(s)

        # dominant_category：占比最高的可演化类别
        evolvable_cats = {
            cat: count for cat, count in cats.items()
            if cat not in _NON_EVOLVABLE_CATEGORIES
        }
        dominant = (
            max(evolvable_cats, key=evolvable_cats.get) if evolvable_cats else None
        )

        return FailureStats(
            by_category=dict(cats),
            dominant_category=dominant,
            sample_failures=sample_failures,
        )