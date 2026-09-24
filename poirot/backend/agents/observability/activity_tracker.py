"""RunActivityTracker — 追踪运行期活动，用于可观测性。

【整体职责】
把每次模型调用、工具调用、沙箱命令、specialist 调用追踪为一条"活动"，
记录其生命周期（started / finished）与心跳（last_progress_at）。消费者（TUI / CLI）
轮询 get_active() 展示当前活动，并据此检测无进展停滞。

【内容摘要】
- Activity            : 单条活动记录，含生命周期、心跳、状态、输出量与错误。
- RunActivityTracker  : 活动追踪器，管理活动集合与生命周期事件。
- RunActivityTracker.start / finish / heartbeat : 活动生命周期入口。
- RunActivityTracker.get_active / get_recent_completed / get_stale_activities
- RunActivityTracker.total_elapsed / reset
- _emit               : 向回调发送活动事件。

【职责边界】
- 只负责：追踪活动生命周期与心跳、检测停滞、汇总耗时、对外发事件。
- 不负责：活动的业务语义（模型/工具/沙箱调用本身）、事件消费与展示（TUI / CLI）、
  停滞的处置（interrupt_protection）、态势汇总（situation_report）。

【INVARIANT】
- 线程安全依赖 GIL：图执行单线程，故不加锁。
- 活动状态取值：running / ok / error / cancelled。
- 心跳节流：heartbeat 按 heartbeat_interval 节流，避免事件过密。
- 停滞判定：no_progress_duration >= no_progress_threshold 视为 stale。
- 完成活动归档：finish 后从 _activities 移入 _completed（保留最近 limit 条）。
- 事件可选：_on_event 为 None 时 _emit 空操作。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Activity:
    """单条活动记录。

    Attributes:
        activity_id: 活动唯一标识。
        kind: 活动类型，"model" | "tool" | "sandbox" | "specialist"。
        summary: 活动摘要。
        started_at: 开始时间戳。
        last_progress_at: 最近进展时间戳（心跳）。
        status: 状态，"running" | "ok" | "error" | "cancelled"。
        output_size: 输出大小。
        finished_at: 结束时间戳，未结束为 None。
        error: 错误信息，仅失败时填。
    """

    activity_id: str
    kind: str  # "model" | "tool" | "sandbox" | "specialist"
    summary: str
    started_at: float
    last_progress_at: float
    status: str = "running"  # "running" | "ok" | "error" | "cancelled"
    output_size: int = 0
    finished_at: float | None = None
    error: str | None = None

    @property
    def elapsed(self) -> float:
        """已耗时（秒）：从开始到结束（未结束则到当前）。"""
        end = self.finished_at or time.time()
        return end - self.started_at

    @property
    def no_progress_duration(self) -> float:
        """无进展时长（秒）：距最近一次进展的时间。"""
        return time.time() - self.last_progress_at


class RunActivityTracker:
    """追踪运行期活动。线程安全依赖 GIL（图执行单线程）。

    Attributes:
        _heartbeat_interval: 心跳事件节流间隔（秒）。
        _no_progress_threshold: 无进展停滞阈值（秒）。
        _on_event: 事件回调，可选。
        _activities: 进行中的活动（activity_id → Activity）。
        _completed: 已完成活动归档。
        _last_heartbeat_emit: 各活动上次发心跳事件的时间。
    """

    def __init__(
        self,
        heartbeat_interval: float = 10.0,
        no_progress_threshold: float = 180.0,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """初始化。

        Args:
            heartbeat_interval: 心跳事件节流间隔（秒）。
            no_progress_threshold: 无进展停滞阈值（秒）。
            on_event: 事件回调，收到活动事件 dict。
        """
        self._heartbeat_interval = heartbeat_interval
        self._no_progress_threshold = no_progress_threshold
        self._on_event = on_event
        self._activities: dict[str, Activity] = {}
        self._completed: list[Activity] = []
        self._last_heartbeat_emit: dict[str, float] = {}

    def start(self, activity_id: str, kind: str, summary: str) -> Activity:
        """开始一条活动，登记并发 activity.started 事件。

        Args:
            activity_id: 活动唯一标识。
            kind: 活动类型。
            summary: 活动摘要。

        Returns:
            Activity: 新建的活动记录。
        """
        now = time.time()
        activity = Activity(
            activity_id=activity_id, kind=kind, summary=summary,
            started_at=now, last_progress_at=now,
        )
        self._activities[activity_id] = activity
        self._emit("activity.started", activity)
        return activity

    def finish(
        self, activity_id: str, status: str = "ok",
        error: str | None = None, output_size: int = 0,
    ) -> Activity | None:
        """结束一条活动，归档并发 activity.finished 事件。

        Args:
            activity_id: 活动唯一标识。
            status: 结束状态。
            error: 错误信息，可选。
            output_size: 输出大小。

        Returns:
            Activity | None: 结束的活动；不存在则 None。
        """
        activity = self._activities.pop(activity_id, None)
        if activity is None:
            return None
        activity.status = status
        activity.error = error
        activity.output_size = output_size
        activity.finished_at = time.time()
        self._completed.append(activity)
        self._last_heartbeat_emit.pop(activity_id, None)
        self._emit("activity.finished", activity)
        return activity

    def heartbeat(self, activity_id: str, output_size: int = 0) -> None:
        """刷新活动进展；按 heartbeat_interval 节流发心跳事件。

        Args:
            activity_id: 活动唯一标识。
            output_size: 当前输出大小。
        """
        activity = self._activities.get(activity_id)
        if activity is None:
            return
        activity.last_progress_at = time.time()
        activity.output_size = output_size
        last_emit = self._last_heartbeat_emit.get(activity_id, 0)
        if time.time() - last_emit >= self._heartbeat_interval:
            self._last_heartbeat_emit[activity_id] = time.time()
            self._emit("activity.heartbeat", activity)

    def get_active(self) -> list[Activity]:
        """返回进行中的活动列表。"""
        return list(self._activities.values())

    def get_recent_completed(self, limit: int = 8) -> list[Activity]:
        """返回最近完成的活动（最多 limit 条）。"""
        return self._completed[-limit:]

    def get_stale_activities(self) -> list[Activity]:
        """返回无进展超过阈值的活动（疑似停滞）。"""
        return [
            a for a in self._activities.values()
            if a.no_progress_duration >= self._no_progress_threshold
        ]

    def total_elapsed(self) -> float:
        """返回所有活动覆盖的总耗时（最早开始到最晚结束）。"""
        if not self._completed and not self._activities:
            return 0.0
        starts = [a.started_at for a in self._completed + list(self._activities.values())]
        ends = [a.finished_at or time.time() for a in self._completed + list(self._activities.values())]
        return max(ends) - min(starts) if starts else 0.0

    def reset(self) -> None:
        """清空全部活动与心跳状态。"""
        self._activities.clear()
        self._completed.clear()
        self._last_heartbeat_emit.clear()

    def _emit(self, event_type: str, activity: Activity) -> None:
        """向回调发送活动事件（_on_event 为 None 时跳过）。

        Args:
            event_type: 事件类型（activity.started / finished / heartbeat）。
            activity: 活动记录。
        """
        if self._on_event is None:
            return
        self._on_event({
            "type": event_type,
            "activity_id": activity.activity_id,
            "kind": activity.kind,
            "summary": activity.summary,
            "elapsed": round(activity.elapsed, 1),
            "status": activity.status,
            "output_size": activity.output_size,
        })