"""L3 SpecialistRuntimeTracker — specialist 健康监控 + 趋势判定 + degraded 检测。

【整体职责】
读 L1 的指标聚合（通过 L2 MetricsView Protocol），算出 specialist 的健康报告
（completion_rate / cost / latency / fallback / trend），并检测哪些 specialist
已 degraded；degraded 命中时把消息 enqueue 到 L2 cron queue，由 L2 消费触发进化。

【内容摘要】
- _MIN_JUDGMENTS_FOR_TREND / _DEGRADATION_DELTA : 趋势判定最小样本量 + 退化阈值常量。
- SpecialistRuntimeTracker                      : 健康监控主类。
- health_report()                               : 生成单个 specialist 的健康报告。
- degraded_specialists()                        : 列出已 degraded 的 specialist。
- trigger_l2_evolution_if_degraded()            : degraded 命中 → enqueue L2 cron queue。

【职责边界】
- 只负责：读指标、算健康报告、判定 degraded、enqueue 触发消息。
- 不负责：指标存储（L1 metrics 负责）、进化执行（L2 负责）、
  lesson 读写（decision_log 负责）。
- 不持有运行时状态：metrics_view 由构造注入。

【INVARIANT】
- 自建类型：不复用 skill 的 RuntimeTracker（返回类型字段不兼容）。
- 复用 L2 MetricsView Protocol：读 get_specialist_metrics / list_specialists。
- 常量与 skill 同构：_MIN_JUDGMENTS_FOR_TREND=4 / _DEGRADATION_DELTA=0.15。
- trend 判定（MVP）：
  - sample_size < 4 → "insufficient_data"
  - 否则 → "stable"
  - （真正的"前后两半比较"留待 MetricsView 支持历史序列后再补）
- degraded 判定：completion_rate < threshold 且 total_invoked >= 5。
- health_report 返回 None：specialist 不存在或无记录。
- trigger_l2_evolution_if_degraded 只 enqueue，不直接调 L2 TriggerManager（解耦）。
- cron_queue 消息格式：("specialist_degraded", name)。
- trigger 函数返回 degraded name 列表（空列表表示无 degraded）。
"""
from __future__ import annotations

import queue
from typing import Any

from poirot.backend.agents.multiagent.eval.types import SpecialistHealthReport
from poirot.backend.agents.multiagent.evolution.metrics_view import MetricsView

# 趋势判定所需的最小样本量。
_MIN_JUDGMENTS_FOR_TREND = 4
# 退化判定默认阈值（与 skill 同构）。
_DEGRADATION_DELTA = 0.15


class SpecialistRuntimeTracker:
    """specialist 健康监控 + 趋势判定 + degraded 检测。

    自建（不复用 skill 的 RuntimeTracker——返回类型字段不兼容）。
    复用 L2 MetricsView Protocol 读 L1 specialist 指标。
    MVP 的 trend 基于 sample_size 判定（< 4 = insufficient_data，否则 stable）。
    """

    def __init__(
        self,
        metrics_view: MetricsView,
        degradation_delta: float = _DEGRADATION_DELTA,
    ) -> None:
        """初始化。

        Args:
            metrics_view: L2 MetricsView Protocol 实现（读 L1 指标）。
            degradation_delta: 退化判定阈值；默认 _DEGRADATION_DELTA。
        """
        self._metrics = metrics_view
        self._degradation_delta = degradation_delta

    def health_report(
        self, specialist_name: str, window: int = 20,
    ) -> SpecialistHealthReport | None:
        """读 MetricsView 算趋势 + 构造 SpecialistHealthReport。

        - 返回 None 表示 specialist 不存在或无记录。
        - MVP trend：sample_size < _MIN_JUDGMENTS_FOR_TREND → "insufficient_data"；
          否则 → "stable"。
        - fallback_rate：total_fallbacks / total_invoked；invoked=0 时 0.0。

        Args:
            specialist_name: specialist 名。
            window: 窗口大小（MVP 未使用，预留）。

        Returns:
            SpecialistHealthReport，或 None。
        """
        snapshot = self._metrics.get_specialist_metrics(specialist_name)
        if snapshot is None:
            return None

        if snapshot["sample_size"] < _MIN_JUDGMENTS_FOR_TREND:
            trend = "insufficient_data"
        else:
            trend = "stable"

        fallback_rate = (
            snapshot["total_fallbacks"] / snapshot["total_invoked"]
            if snapshot["total_invoked"] > 0
            else 0.0
        )

        return SpecialistHealthReport(
            specialist_name=specialist_name,
            window_invoked=snapshot["sample_size"],
            completion_rate=snapshot["completion_rate"],
            avg_cost_usd=snapshot["avg_cost_usd"],
            avg_latency_seconds=snapshot["avg_latency_seconds"],
            fallback_rate=fallback_rate,
            trend=trend,  # type: ignore[arg-type]
            advice="",
        )

    def degraded_specialists(self, threshold: float = 0.4) -> list[str]:
        """列出已 degraded 的 specialist。

        判定条件：total_invoked >= 5 且 completion_rate < threshold。

        Args:
            threshold: 完成率阈值，默认 0.4。

        Returns:
            degraded specialist 名列表。
        """
        degraded: list[str] = []
        for name in self._metrics.list_specialists():
            snapshot = self._metrics.get_specialist_metrics(name)
            if snapshot is None:
                continue
            if (
                snapshot["total_invoked"] >= 5
                and snapshot["completion_rate"] < threshold
            ):
                degraded.append(name)
        return degraded


def trigger_l2_evolution_if_degraded(
    tracker: SpecialistRuntimeTracker,
    cron_queue: queue.Queue,
    threshold: float = 0.4,
) -> list[str]:
    """degraded 命中 → enqueue L2 cron queue（不直接调 L2 TriggerManager，解耦）。

    Args:
        tracker: 健康追踪器。
        cron_queue: L2 cron 队列。
        threshold: 完成率阈值，默认 0.4。

    Returns:
        degraded specialist name 列表（空列表表示无 degraded）。
    """
    degraded = tracker.degraded_specialists(threshold=threshold)
    for name in degraded:
        cron_queue.put(("specialist_degraded", name))
    return degraded