"""OrchestrationMetricsL2 — L2 演化过程指标（11 种事件类型）。

【整体职责】
记录 L2 进化过程的 11 类事件（trigger / evolution_start / mutator_call /
evolution_failed / promotion_decision / eval_executed / eval_failed /
version_rollback / blocked_marked / budget_warning / budget_exceeded），
写入 multiagent.db 的 l2_metrics 表，供 CLI inspect（无主动推送）。

【内容摘要】
- 11 个 EVENT_* 常量 + _ALL_EVENT_TYPES : 事件类型定义。
- _SCHEMA_SQL                           : l2_metrics 表建表 SQL（幂等）。
- OrchestrationMetricsL2                : 指标写入主类（11 个 record_* 方法 + query）。
- query_by_event_type()                 : 按事件类型查询（CLI inspect 用）。
- event_types (property)                : 暴露 11 种事件类型 tuple。

【职责边界】
- 只负责：事件写入 l2_metrics 表、按类型查询。
- 不负责：指标消费决策（L2 各组件负责）、主动推送（无，用户主动 inspect）、
  与 L1 的 specialist call 指标混合（两者独立）。
- 不持有运行时状态：只持有 db_path + 锁。

【INVARIANT】
- 写 l2_metrics 表，不写 ThreadState。
- 11 种事件类型固定，不增不减。
- 无主动推送：用户通过 CLI inspect（query_by_event_type）。
- 与 L1 OrchestrationMetrics 独立：L1 记 specialist call，L2 记演化过程。
- event_type 加 l3_ 前缀可复用给 L3（同一张表）。
- 每条事件：metric_id + event_type + payload_json + timestamp。
- payload_json 用 sort_keys=True 序列化（保证可复现）。
- WAL 模式 + busy_timeout=30000 + threading.Lock 保护写入。
- 幂等建表：CREATE TABLE IF NOT EXISTS。
- metric_id 格式：m2_<12 位 hex>。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path

from poirot.backend.agents.journal.events import utc_now_iso

# L2 的 11 种事件类型。
EVENT_TRIGGER = "trigger"
EVENT_EVOLUTION_START = "evolution_start"
EVENT_MUTATOR_CALL = "mutator_call"
EVENT_EVOLUTION_FAILED = "evolution_failed"
EVENT_PROMOTION_DECISION = "promotion_decision"
EVENT_EVAL_EXECUTED = "eval_executed"
EVENT_EVAL_FAILED = "eval_failed"
EVENT_VERSION_ROLLBACK = "version_rollback"
EVENT_BLOCKED_MARKED = "blocked_marked"
EVENT_BUDGET_WARNING = "budget_warning"
EVENT_BUDGET_EXCEEDED = "budget_exceeded"

_ALL_EVENT_TYPES = (
    EVENT_TRIGGER,
    EVENT_EVOLUTION_START,
    EVENT_MUTATOR_CALL,
    EVENT_EVOLUTION_FAILED,
    EVENT_PROMOTION_DECISION,
    EVENT_EVAL_EXECUTED,
    EVENT_EVAL_FAILED,
    EVENT_VERSION_ROLLBACK,
    EVENT_BLOCKED_MARKED,
    EVENT_BUDGET_WARNING,
    EVENT_BUDGET_EXCEEDED,
)

# l2_metrics 表建表 SQL（幂等）。
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS l2_metrics (
    metric_id   TEXT PRIMARY KEY,
    event_type  TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    timestamp   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_l2_metrics_event_type ON l2_metrics(event_type);
CREATE INDEX IF NOT EXISTS idx_l2_metrics_timestamp ON l2_metrics(timestamp);
"""


class OrchestrationMetricsL2:
    """L2 演化过程指标（11 种事件类型）。

    - 写 multiagent.db 的 l2_metrics 表（不写 ThreadState）。
    - 11 种事件类型。
    - 无主动推送：用户通过 CLI inspect。
    """

    def __init__(self, db_path: str = ".poirot/multiagent.db") -> None:
        """初始化：建父目录 + 幂等建表。"""
        self._db_path = db_path
        self._lock = threading.Lock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        """建立连接并设置 WAL + busy_timeout。每次调用返回新连接。"""
        conn = sqlite3.connect(self._db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_schema(self) -> None:
        """幂等建表（CREATE TABLE IF NOT EXISTS）。"""
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA_SQL)
                conn.commit()
            finally:
                conn.close()

    def record_trigger(
        self,
        trigger_source: str,
        trigger_detail: str = "",
        profile: str = "default",
    ) -> None:
        """记录 trigger 事件（触发源 + 详情 + profile）。"""
        self._record(EVENT_TRIGGER, {
            "trigger_source": trigger_source,
            "trigger_detail": trigger_detail,
            "profile": profile,
        })

    def record_evolution_start(
        self,
        experiment_id: str,
        artifact_type: str,
        from_version: str = "",
    ) -> None:
        """记录 evolution_start 事件（实验 ID + 产物类型 + 父版本）。"""
        self._record(EVENT_EVOLUTION_START, {
            "experiment_id": experiment_id,
            "artifact_type": artifact_type,
            "from_version": from_version,
        })

    def record_mutator_call(
        self,
        llm_model: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: float = 0.0,
        retries: int = 0,
    ) -> None:
        """记录 mutator_call 事件（EvolutionMutator 的 LLM 调用）。"""
        self._record(EVENT_MUTATOR_CALL, {
            "llm_model": llm_model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "latency_ms": latency_ms,
            "retries": retries,
        })

    def record_evolution_failed(
        self,
        failure_type: str,
        experiment_id: str = "",
        detail: str = "",
    ) -> None:
        """记录 evolution_failed 事件。

        failure_type：llm_timeout / json_parse / schema_mismatch / illegal_field。
        """
        self._record(EVENT_EVOLUTION_FAILED, {
            "failure_type": failure_type,
            "experiment_id": experiment_id,
            "detail": detail,
        })

    def record_promotion_decision(
        self,
        decision: str,
        candidate_score: float = 0.0,
        baseline_score: float = 0.0,
        ci_low: float = 0.0,
        ci_high: float = 0.0,
    ) -> None:
        """记录 promotion_decision 事件（accept / reject）。"""
        self._record(EVENT_PROMOTION_DECISION, {
            "decision": decision,
            "candidate_score": candidate_score,
            "baseline_score": baseline_score,
            "ci_low": ci_low,
            "ci_high": ci_high,
        })

    def record_eval_executed(
        self,
        eval_method: str = "",
        sample_count: int = 0,
        eval_duration_ms: float = 0.0,
        eval_skipped_count: int = 0,
    ) -> None:
        """记录 eval_executed 事件（评估方法 + 样本数 + 耗时 + 跳过数）。"""
        self._record(EVENT_EVAL_EXECUTED, {
            "eval_method": eval_method,
            "sample_count": sample_count,
            "eval_duration_ms": eval_duration_ms,
            "eval_skipped_count": eval_skipped_count,
        })

    def record_eval_failed(
        self,
        eval_failure_type: str,
        detail: str = "",
    ) -> None:
        """记录 eval_failed 事件。

        eval_failure_type：task_timeout / sandbox_error / overall_timeout。
        """
        self._record(EVENT_EVAL_FAILED, {
            "eval_failure_type": eval_failure_type,
            "detail": detail,
        })

    def record_version_rollback(
        self,
        rollback_from: str = "",
        rollback_to: str = "",
        reason: str = "",
    ) -> None:
        """记录 version_rollback 事件（从哪个版本回滚到哪个版本 + 原因）。"""
        self._record(EVENT_VERSION_ROLLBACK, {
            "rollback_from": rollback_from,
            "rollback_to": rollback_to,
            "reason": reason,
        })

    def record_blocked_marked(
        self,
        blocked_pattern: str,
        blocked_type: str = "evolution",
        auto_release_at: str = "",
    ) -> None:
        """记录 blocked_marked 事件（blocked_type：evolution_blocked / eval_blocked）。"""
        self._record(EVENT_BLOCKED_MARKED, {
            "blocked_pattern": blocked_pattern,
            "blocked_type": blocked_type,
            "auto_release_at": auto_release_at,
        })

    def record_budget_warning(
        self,
        specialist_name: str,
        usage_percent: float = 0.0,
        remaining_cost_usd: float = 0.0,
    ) -> None:
        """记录 budget_warning 事件（80% 用量告警）。"""
        self._record(EVENT_BUDGET_WARNING, {
            "specialist_name": specialist_name,
            "usage_percent": usage_percent,
            "remaining_cost_usd": remaining_cost_usd,
        })

    def record_budget_exceeded(
        self,
        specialist_name: str,
        exceeded_dimension: str = "cost_usd",
        fallback_target: str = "lead",
    ) -> None:
        """记录 budget_exceeded 事件（超限维度 + fallback 目标）。"""
        self._record(EVENT_BUDGET_EXCEEDED, {
            "specialist_name": specialist_name,
            "exceeded_dimension": exceeded_dimension,
            "fallback_target": fallback_target,
        })

    def _record(self, event_type: str, payload: dict) -> None:
        """写事件到 l2_metrics 表（不写 ThreadState）。

        - metric_id 格式：m2_<12 位 hex>。
        - payload_json 用 sort_keys=True 序列化。
        """
        metric_id = f"m2_{uuid.uuid4().hex[:12]}"
        payload_json = json.dumps(payload, sort_keys=True)
        now = utc_now_iso()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """INSERT INTO l2_metrics (metric_id, event_type, payload_json, timestamp)
                        VALUES (?, ?, ?, ?)""",
                    (metric_id, event_type, payload_json, now),
                )
                conn.commit()
            finally:
                conn.close()

    def query_by_event_type(self, event_type: str, limit: int = 100) -> list[dict]:
        """按事件类型查询（CLI inspect 用）。

        Args:
            event_type: 事件类型。
            limit: 最多返回条数，默认 100。

        Returns:
            事件 dict 列表（含 metric_id / event_type / payload / timestamp）。
        """
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """SELECT metric_id, event_type, payload_json, timestamp
                        FROM l2_metrics WHERE event_type=? ORDER BY timestamp DESC LIMIT ?""",
                    (event_type, limit),
                ).fetchall()
            finally:
                conn.close()
        return [
            {
                "metric_id": r[0],
                "event_type": r[1],
                "payload": json.loads(r[2]),
                "timestamp": r[3],
            }
            for r in rows
        ]

    @property
    def event_types(self) -> tuple[str, ...]:
        """暴露 11 种 L2 事件类型 tuple。"""
        return _ALL_EVENT_TYPES