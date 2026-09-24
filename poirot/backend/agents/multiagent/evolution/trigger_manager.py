"""TriggerManager — 四源触发 + 1h 冷却 + per-profile 串行。

【整体职责】
决定 L2 进化"什么时候触发"：用纯数值判断检查四类触发源（失败聚焦 / specialist 降级 /
metric 阈值 / 周期兜底），命中且不在冷却窗口内时记录触发信息并允许 enqueue
EvolutionTask 到共享队列。

【内容摘要】
- TriggerThresholds(dataclass) : 触发阈值配置（可覆盖默认值）。
- TriggerState(dataclass)      : per-profile 触发状态（冷却 + last_trigger 记录）。
- TriggerManager               : 触发管理主类（should_trigger / _check_sources / enqueue）。

【职责边界】
- 只负责：纯数值判断是否触发、记录触发状态、enqueue 任务。
- 不负责：任务执行（worker 负责）、具体演化逻辑（mutator 负责）、
  周期兜底判定（worker loop 负责）。
- 不持有重状态：只持有 queue / thresholds / states dict + 锁。

【INVARIANT】
- should_trigger 是纯数值判断：不调 LLM。
- 四源检查顺序固定：失败聚焦 → specialist 降级 → metric 阈值 → 周期兜底。
- 1h 冷却窗口（per-profile）：窗口内不重复触发。
- per-profile 串行由 daemon 单线程消费保证，本类不加额外锁。
- 不可演化类（GOAL_UNCLEAR / SANDBOX_ISSUE）不触发失败聚焦。
- 周期兜底不由 TriggerManager 主动判定：由 worker loop 的 sleep 兜底。
- 命中时记录 last_trigger_source / last_trigger_detail（供 Middleware 读取）。
- 降级判定：total_invoked >= 5 且 completion_rate < 0.4。
- metric 告警：单次 cost > $1 或 latency > 5min。
- 阈值可配置：TriggerThresholds 默认值可被构造参数覆盖。
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from poirot.backend.agents.multiagent.evolution.metrics_view import MetricsView
from poirot.backend.agents.multiagent.evolution.types import (
    EvolutionTask,
    FailureCategory,
    TriggerSource,
)


@dataclass
class TriggerThresholds:
    """触发阈值（可配置覆盖默认值）。

    - failure_window_seconds: 失败聚焦统计窗口（默认 24h = 86400s）。
    - failure_threshold: 失败聚焦触发阈值（默认 ≥5 次）。
    - degradation_min_invoked: specialist 降级最小调用次数（默认 ≥5）。
    - degradation_threshold: specialist 降级 completion_rate 阈值（默认 <0.4）。
    - cost_alert_usd: 单次 cost 告警（默认 >$1）。
    - latency_alert_seconds: 单次 latency 告警（默认 >5min=300s）。
    """

    failure_window_seconds: float = 86400.0  # 24h
    failure_threshold: int = 5
    degradation_min_invoked: int = 5
    degradation_threshold: float = 0.4
    cost_alert_usd: float = 1.0
    latency_alert_seconds: float = 300.0


@dataclass
class TriggerState:
    """per-profile 触发状态（冷却 + last_trigger 记录）。

    - last_trigger_ts: 上次触发时间戳（用于 1h 冷却）。
    - last_trigger_source: 上次触发的 source（enqueue 时用）。
    - last_trigger_detail: 上次触发详情（enqueue 时用）。
    """

    last_trigger_ts: float = 0.0
    last_trigger_source: TriggerSource | None = None
    last_trigger_detail: str = ""


class TriggerManager:
    """四源触发 + 1h 冷却 + per-profile 串行。

    should_trigger 纯数值判断（不调 LLM）。
    enqueue 写 queue.Queue（daemon thread 消费，per-profile 串行）。
    1h 冷却窗口防抖（per-profile 不重复触发）。
    """

    def __init__(
        self,
        task_queue: "queue.Queue[EvolutionTask]",
        cooldown_seconds: float = 3600.0,  # 1h
        cron_interval_seconds: float = 21600.0,  # 6h
        thresholds: TriggerThresholds | None = None,
    ) -> None:
        """初始化。

        Args:
            task_queue: 共享任务队列（worker 消费）。
            cooldown_seconds: 冷却窗口（秒），默认 1h。
            cron_interval_seconds: 周期兜底间隔（秒），默认 6h。
            thresholds: 触发阈值；None 时用 TriggerThresholds 默认值。
        """
        self._queue = task_queue
        self._cooldown_seconds = cooldown_seconds
        self._cron_interval_seconds = cron_interval_seconds
        self._thresholds = thresholds or TriggerThresholds()
        self._lock = threading.Lock()
        self._states: dict[str, TriggerState] = {}
        # last_trigger_source 供 L2TriggerMiddleware._enqueue_evolution_task 读取
        self.last_trigger_source: TriggerSource | None = None
        self.last_trigger_detail: str = ""

    def should_trigger(
        self,
        metrics_view: MetricsView,
        profile: str = "default",
    ) -> bool:
        """纯数值检查：四源 + 1h 冷却（不调 LLM）。

        - 命中且不在冷却窗口 → 返回 True + 记录 last_trigger_source/detail。
        - 冷却未过期 → 返回 False（不重复触发）。
        - 无源命中 → 返回 False。

        Args:
            metrics_view: L2 MetricsView Protocol（读 L1 指标）。
            profile: 演化 profile（per-profile 冷却 key）。

        Returns:
            是否应触发。
        """
        now = time.time()
        with self._lock:
            state = self._states.setdefault(profile, TriggerState())
            # 1h 冷却窗口（per-profile 不重复触发）
            if now - state.last_trigger_ts < self._cooldown_seconds:
                return False
            # 四源检查
            source, detail = self._check_sources(metrics_view, now)
            if source is None:
                return False
            state.last_trigger_ts = now
            state.last_trigger_source = source
            state.last_trigger_detail = detail
            self.last_trigger_source = source
            self.last_trigger_detail = detail
            return True

    def _check_sources(
        self,
        metrics_view: MetricsView,
        now: float,
    ) -> tuple[TriggerSource | None, str]:
        """四源检查（失败聚焦 / specialist 降级 / metric 告警 / 周期兜底）。

        返回 (source, detail)；source=None 表示无触发。
        顺序：
        1. 失败聚焦：24h 窗口内某可演化 failure_category ≥ 5。
        2. specialist 降级：invoked ≥ 5 且 completion_rate < 0.4。
        3. metric 阈值：单次 cost > $1 或 latency > 5min。
        4. 周期兜底：由 worker loop 兜底，本方法不主动判定。
        """
        # 1. 失败聚焦（24h 窗口内某 failure_category ≥ 5）
        since = now - self._thresholds.failure_window_seconds
        cats = metrics_view.get_failure_categories(since=since)
        for cat, count in cats.items():
            if cat in (FailureCategory.GOAL_UNCLEAR, FailureCategory.SANDBOX_ISSUE):
                continue  # 不可演化类不触发
            if count >= self._thresholds.failure_threshold:
                return (
                    TriggerSource.FAILURE_FOCUSED,
                    f"{cat.value}={count} in last 24h",
                )

        # 2. specialist 降级（invoked ≥ 5 + completion_rate < 0.4）
        for name in metrics_view.list_specialists():
            snap = metrics_view.get_specialist_metrics(name, since=since)
            if snap is None:
                continue
            if (
                snap["total_invoked"] >= self._thresholds.degradation_min_invoked
                and snap["completion_rate"] < self._thresholds.degradation_threshold
            ):
                return (
                    TriggerSource.SPECIALIST_DEGRADED,
                    f"{name}: rate={snap['completion_rate']:.2f} invoked={snap['total_invoked']}",
                )

        # 3. metric 阈值（单次 cost > $1 或 latency > 5min）
        global_snap = metrics_view.get_global_metrics(since=since)
        if global_snap["total_cost_usd"] > self._thresholds.cost_alert_usd:
            return (
                TriggerSource.COST_ALERT,
                f"cost=${global_snap['total_cost_usd']:.2f}",
            )
        if global_snap["avg_latency_seconds"] > self._thresholds.latency_alert_seconds:
            return (
                TriggerSource.LATENCY_ALERT,
                f"latency={global_snap['avg_latency_seconds']:.0f}s",
            )

        # 4. 周期兜底（6h cron）——由 L2EvolutionWorker worker loop 兜底，
        # TriggerManager 不主动判定。
        return (None, "")

    def enqueue(self, task: EvolutionTask) -> None:
        """enqueue EvolutionTask 到 queue（daemon thread 消费，per-profile 串行）。

        per-profile 串行由 daemon thread 单线程消费保证，本方法不加额外锁。

        Args:
            task: 演化任务。
        """
        self._queue.put(task)