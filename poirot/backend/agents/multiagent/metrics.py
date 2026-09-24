"""MultiAgentMetricsStore — multiagent 编排的 SQLite 指标存储。

【整体职责】
以 SQLite 持久化 multiagent 的运行指标与辅助数据，供健康检查、L2 进化、L3 评估读取。
存储 9 张表：specialist 计数器、judgment 明细、进化产物/实验、L2 指标、
预算用量/告警、L2 阻断模式、决策日志（主表 + 归档表）。

【内容摘要】
- _SCHEMA_VERSION / _SCHEMA_SQL  : schema 版本与建表 SQL（9 张表）。
- SpecialistMetrics              : specialist 聚合指标（从 specialist_records 派生）。
- SpecialistHealth               : specialist 健康状态（health_check 输出）。
- MultiAgentMetricsStore         : 存储主类，提供打点、查询、健康检查、决策日志、归档等方法。

【职责边界】
- 只负责：SQLite 建表、打点写入、指标查询、决策日志存取与归档。
- 不负责：指标消费决策（health_check 只判定，不触发动作）、L2/L3 业务逻辑
  （通过 MetricsView 协议暴露数据给它们）、specialist 执行。
- 不进 ThreadState：指标与线程状态分离。

【INVARIANT】
- WAL 模式 + busy_timeout=30000 + threading.Lock 保护写：保证并发安全。
- PRAGMA user_version 记 schema 版本，首次启动建表；版本过低时重建。
- 4 计数器 programmatic 打点：selection / invoked / completion / fallback。
- 所有写操作持锁；读操作也持锁（简化并发，代价可接受）。
- completion_rate / fallback_rate 分母为 0 时返回 0.0，不抛异常。
- failure_category 以字符串存储（enum .value），避免跨层 import。
- L2 / L3 类型在方法内 lazy import：避免循环依赖。
- 决策日志归档：先复制到 archive 表，再删主表；返回归档条数。
- 成本估算为占位实现：tokens * 0.00002，真实价格应从 config 取。
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from poirot.backend.agents.journal.events import utc_now_iso

# schema 版本号。低于此版本会触发建表 / 升级。
_SCHEMA_VERSION = 3

# 建表 SQL：9 张表 + 索引。全部 IF NOT EXISTS，可重复执行。
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS specialist_records (
    specialist_name     TEXT PRIMARY KEY,
    total_selections    INTEGER NOT NULL DEFAULT 0,
    total_invoked       INTEGER NOT NULL DEFAULT 0,
    total_completions   INTEGER NOT NULL DEFAULT 0,
    total_fallbacks     INTEGER NOT NULL DEFAULT 0,
    failure_category    TEXT,
    created_at          TEXT NOT NULL,
    last_updated        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS specialist_judgments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT NOT NULL,
    specialist_name     TEXT NOT NULL,
    success             INTEGER NOT NULL DEFAULT 0,
    duration_seconds    REAL NOT NULL DEFAULT 0.0,
    prompt_tokens       INTEGER NOT NULL DEFAULT 0,
    completion_tokens   INTEGER NOT NULL DEFAULT 0,
    cost_usd            REAL NOT NULL DEFAULT 0.0,
    failure_category    TEXT,
    gap_analysis        TEXT NOT NULL DEFAULT '',
    ts                  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sj_name ON specialist_judgments(specialist_name);
CREATE INDEX IF NOT EXISTS idx_sj_run  ON specialist_judgments(run_id);

CREATE TABLE IF NOT EXISTS evolution_artifacts (
    artifact_id     TEXT PRIMARY KEY,
    artifact_type   TEXT NOT NULL,
    version         TEXT NOT NULL,
    template_id     TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    artifact_hash   TEXT NOT NULL,
    rationale       TEXT,
    created_at      TEXT NOT NULL,
    is_active       INTEGER DEFAULT 0,
    UNIQUE(artifact_type, template_id, version)
);

CREATE TABLE IF NOT EXISTS evolution_experiments (
    experiment_id   TEXT PRIMARY KEY,
    artifact_id      TEXT NOT NULL REFERENCES evolution_artifacts(artifact_id),
    from_artifact_id TEXT,
    trigger_source   TEXT NOT NULL,
    trigger_detail   TEXT,
    eval_method      TEXT,
    eval_result_json TEXT,
    decision         TEXT NOT NULL,
    timestamp        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evo_exp_artifact ON evolution_experiments(artifact_id);

CREATE TABLE IF NOT EXISTS l2_metrics (
    metric_id   TEXT PRIMARY KEY,
    event_type  TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    timestamp   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_l2_metrics_event_type ON l2_metrics(event_type);
CREATE INDEX IF NOT EXISTS idx_l2_metrics_timestamp ON l2_metrics(timestamp);

CREATE TABLE IF NOT EXISTS specialist_budget_usage (
    specialist_name TEXT NOT NULL,
    date            TEXT NOT NULL,
    tokens_used     INTEGER DEFAULT 0,
    cost_usd_used   REAL DEFAULT 0.0,
    calls_used      INTEGER DEFAULT 0,
    last_updated    TEXT NOT NULL,
    PRIMARY KEY (specialist_name, date)
);

CREATE TABLE IF NOT EXISTS budget_warnings (
    warning_id      TEXT PRIMARY KEY,
    specialist_name TEXT NOT NULL,
    date            TEXT NOT NULL,
    warning_type    TEXT NOT NULL,
    detail          TEXT,
    timestamp       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_budget_warnings_name_date ON budget_warnings(specialist_name, date);

CREATE TABLE IF NOT EXISTS l2_blocked_patterns (
    blocked_id       TEXT PRIMARY KEY,
    blocked_type     TEXT NOT NULL,
    pattern_key      TEXT NOT NULL,
    reason           TEXT,
    blocked_at       TEXT NOT NULL,
    auto_release_at  TEXT NOT NULL,
    released         INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS specialist_decision_log (
    log_id                  TEXT PRIMARY KEY,
    specialist_name         TEXT NOT NULL,
    task_id                 TEXT NOT NULL,
    goal                    TEXT NOT NULL,
    success_criteria        TEXT NOT NULL,
    failure_category        TEXT,
    success_criteria_met    INTEGER,
    lesson_text             TEXT,
    timestamp               TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decision_log_specialist ON specialist_decision_log(specialist_name, timestamp);
CREATE INDEX IF NOT EXISTS idx_decision_log_category ON specialist_decision_log(failure_category, timestamp);

CREATE TABLE IF NOT EXISTS specialist_decision_log_archive (
    log_id                  TEXT PRIMARY KEY,
    specialist_name         TEXT NOT NULL,
    task_id                 TEXT NOT NULL,
    goal                    TEXT NOT NULL,
    success_criteria        TEXT NOT NULL,
    failure_category        TEXT,
    success_criteria_met    INTEGER,
    lesson_text             TEXT,
    timestamp               TEXT NOT NULL,
    archived_at             TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class SpecialistMetrics:
    """specialist 聚合指标（从 specialist_records 派生）。

    completion_rate / fallback_rate 为派生属性，分母为 0 时返回 0.0。
    """

    specialist_name: str
    total_selections: int
    total_invoked: int
    total_completions: int
    total_fallbacks: int

    @property
    def completion_rate(self) -> float:
        """完成率 = completions / invoked；invoked=0 时返回 0.0。"""
        if self.total_invoked == 0:
            return 0.0
        return self.total_completions / self.total_invoked

    @property
    def fallback_rate(self) -> float:
        """回退率 = fallbacks / invoked；invoked=0 时返回 0.0。"""
        if self.total_invoked == 0:
            return 0.0
        return self.total_fallbacks / self.total_invoked


@dataclass(frozen=True)
class SpecialistHealth:
    """specialist 健康状态（health_check 输出）。

    degraded 判定：completion_rate < threshold 且 total_invoked >= min_invoked。
    """

    specialist_name: str
    completion_rate: float
    total_invoked: int
    degraded: bool


class MultiAgentMetricsStore:
    """multiagent 编排的 SQLite 指标存储。

    INVARIANT:
    - WAL 模式 + busy_timeout=30000 + threading.Lock 保护写。
    - PRAGMA user_version 记 schema 版本，首次启动建表。
    - 4 计数器 programmatic 打点（selection / invoked / completion / fallback）。
    - 指标与 ThreadState 分离，不进 ThreadState。
    """

    def __init__(self, db_path: str = ".poirot/multiagent.db") -> None:
        """打开 / 创建数据库。父目录不存在时自动创建，随后初始化 schema。"""
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
        """按 schema 版本初始化：版本低于当前值时执行建表 SQL 并更新版本号。"""
        with self._lock:
            conn = self._connect()
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version < _SCHEMA_VERSION:
                    conn.executescript(_SCHEMA_SQL)
                    conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                    conn.commit()
            finally:
                conn.close()

    def _upsert_counter(self, specialist_name: str, field: str) -> None:
        """对 specialist_records 的某计数器做 +1 的 upsert（不存在则插入 1）。"""
        now = utc_now_iso()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    f"""INSERT INTO specialist_records (specialist_name, {field}, created_at, last_updated)
                        VALUES (?, 1, ?, ?)
                        ON CONFLICT(specialist_name) DO UPDATE SET
                            {field} = {field} + 1,
                            last_updated = ?""",
                    (specialist_name, now, now, now),
                )
                conn.commit()
            finally:
                conn.close()

    def record_selection(self, specialist_name: str) -> None:
        """打点：specialist 被选中（total_selections +1）。"""
        self._upsert_counter(specialist_name, "total_selections")

    def record_invoked(self, specialist_name: str) -> None:
        """打点：specialist 被实际调用（total_invoked +1）。"""
        self._upsert_counter(specialist_name, "total_invoked")

    def record_completion(self, specialist_name: str) -> None:
        """打点：specialist 成功完成（total_completions +1）。"""
        self._upsert_counter(specialist_name, "total_completions")

    def record_fallback(self, specialist_name: str) -> None:
        """打点：specialist 回退到其他路径（total_fallbacks +1）。"""
        self._upsert_counter(specialist_name, "total_fallbacks")

    def record_judgment(
        self,
        run_id: str,
        specialist_name: str,
        success: bool,
        duration_seconds: float = 0.0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_usd: float = 0.0,
        failure_category: str | None = None,
        gap_analysis: str = "",
    ) -> None:
        """写入一条 specialist judgment 明细（含耗时、token、成本、失败分类）。"""
        now = utc_now_iso()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """INSERT INTO specialist_judgments
                        (run_id, specialist_name, success, duration_seconds,
                         prompt_tokens, completion_tokens, cost_usd,
                         failure_category, gap_analysis, ts)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        run_id, specialist_name, int(success), duration_seconds,
                        prompt_tokens, completion_tokens, cost_usd,
                        failure_category, gap_analysis, now,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def get_metrics(self, specialist_name: str) -> SpecialistMetrics | None:
        """查询单个 specialist 的聚合指标；无记录返回 None。"""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """SELECT specialist_name, total_selections, total_invoked,
                              total_completions, total_fallbacks
                        FROM specialist_records WHERE specialist_name = ?""",
                    (specialist_name,),
                ).fetchone()
            finally:
                conn.close()
        if row is None:
            return None
        return SpecialistMetrics(
            specialist_name=row[0],
            total_selections=row[1],
            total_invoked=row[2],
            total_completions=row[3],
            total_fallbacks=row[4],
        )

    def get_top_specialists(self, limit: int = 10) -> list[SpecialistMetrics]:
        """按 total_invoked 降序取前 N 个 specialist 的聚合指标。"""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """SELECT specialist_name, total_selections, total_invoked,
                              total_completions, total_fallbacks
                        FROM specialist_records
                        ORDER BY total_invoked DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            finally:
                conn.close()
        return [
            SpecialistMetrics(
                specialist_name=r[0], total_selections=r[1], total_invoked=r[2],
                total_completions=r[3], total_fallbacks=r[4],
            )
            for r in rows
        ]

    def health_check(
        self,
        threshold: float = 0.4,
        min_invoked: int = 5,
    ) -> list[SpecialistHealth]:
        """返回所有 specialist 的健康状态。

        degraded 判定：completion_rate < threshold 且 total_invoked >= min_invoked。
        """
        all_metrics = self.get_top_specialists(limit=100)
        return [
            SpecialistHealth(
                specialist_name=m.specialist_name,
                completion_rate=m.completion_rate,
                total_invoked=m.total_invoked,
                degraded=(m.completion_rate < threshold and m.total_invoked >= min_invoked),
            )
            for m in all_metrics
        ]

    # ── MetricsView 协议实现（供 L2 读取 L1 数据） ──

    def get_specialist_metrics(
        self, name: str, *, since: float | None = None
    ) -> dict | None:
        """单个 specialist 的聚合快照（join specialist_judgments 取成本/延迟）。

        取最近 20 条 judgment 算平均延迟与成本；无记录时返回 None。
        """
        base = self.get_metrics(name)
        if base is None:
            return None
        with self._lock:
            conn = self._connect()
            try:
                if since is not None:
                    since_str = utc_now_iso()
                    rows = conn.execute(
                        """SELECT duration_seconds, prompt_tokens, completion_tokens
                            FROM specialist_judgments
                            WHERE specialist_name=? AND ts >= ?
                            ORDER BY ts DESC LIMIT 20""",
                        (name, since_str),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT duration_seconds, prompt_tokens, completion_tokens
                            FROM specialist_judgments
                            WHERE specialist_name=?
                            ORDER BY ts DESC LIMIT 20""",
                        (name,),
                    ).fetchall()
            finally:
                conn.close()
        if rows:
            avg_latency = sum(r[0] for r in rows) / len(rows)
            # 成本估算占位实现：tokens * $0.00002（真实价格应从 config 取）
            total_tokens = sum(r[1] + r[2] for r in rows)
            avg_cost = total_tokens * 0.00002 / len(rows)
            sample_size = len(rows)
        else:
            avg_latency = 0.0
            avg_cost = 0.0
            sample_size = 0
        return {
            "specialist_name": base.specialist_name,
            "total_selections": base.total_selections,
            "total_invoked": base.total_invoked,
            "total_completions": base.total_completions,
            "total_fallbacks": base.total_fallbacks,
            "completion_rate": base.completion_rate,
            "avg_cost_usd": avg_cost,
            "avg_latency_seconds": avg_latency,
            "sample_size": sample_size,
        }

    def get_global_metrics(self, *, since: float | None = None) -> dict:
        """全局聚合快照：所有 specialist 的调用、成本、延迟汇总。"""
        all_metrics = self.get_top_specialists(limit=100)
        total_calls = sum(m.total_invoked for m in all_metrics)
        total_selections = sum(m.total_selections for m in all_metrics)
        total_completions = sum(m.total_completions for m in all_metrics)
        total_fallbacks = sum(m.total_fallbacks for m in all_metrics)
        # 全局平均延迟 / 总成本
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """SELECT AVG(duration_seconds), SUM(prompt_tokens + completion_tokens) * 0.00002
                        FROM specialist_judgments"""
                ).fetchone()
            finally:
                conn.close()
        avg_latency = row[0] if row and row[0] else 0.0
        total_cost = row[1] if row and row[1] else 0.0
        return {
            "total_calls": total_calls,
            "total_cost_usd": total_cost,
            "avg_latency_seconds": avg_latency,
            "total_selections": total_selections,
            "total_completions": total_completions,
            "total_fallbacks": total_fallbacks,
        }

    def get_failure_categories(self, *, since: float | None = None) -> dict:
        """按 failure_category 统计失败次数。

        字符串 category 映射回 FailureCategory 枚举；未知值跳过。
        """
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """SELECT failure_category, COUNT(*)
                        FROM specialist_judgments
                        WHERE failure_category IS NOT NULL
                        GROUP BY failure_category"""
                ).fetchall()
            finally:
                conn.close()
        # 字符串 category → FailureCategory 枚举（lazy import 避免循环依赖）
        from poirot.backend.agents.multiagent.evolution.types import FailureCategory
        result: dict = {}
        for cat_str, count in rows:
            try:
                cat = FailureCategory(cat_str)
                result[cat] = count
            except ValueError:
                continue  # 未知 category 字符串，跳过
        return result

    def get_recent_failures(
        self, *, category, limit: int = 10
    ) -> list:
        """取某 failure_category 下最近 N 条失败记录。"""
        cat_str = category.value if hasattr(category, "value") else str(category)
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """SELECT specialist_name, gap_analysis, failure_category, ts
                        FROM specialist_judgments
                        WHERE failure_category=?
                        ORDER BY ts DESC LIMIT ?""",
                    (cat_str, limit),
                ).fetchall()
            finally:
                conn.close()
        from poirot.backend.agents.multiagent.evolution.types import FailureRecord
        records: list[FailureRecord] = []
        for r in rows:
            try:
                fc = FailureCategory(r[2])
            except ValueError:
                continue
            records.append(FailureRecord(
                specialist_name=r[0],
                goal="",
                success_criteria="",
                failure_category=fc,
                raw_output_tail=r[1] or "",
                timestamp=r[3] or "",
            ))
        return records

    def list_specialists(self) -> list[str]:
        """列出所有有记录的 specialist 名字。"""
        all_metrics = self.get_top_specialists(limit=100)
        return [m.specialist_name for m in all_metrics]

    def save_decision_log(self, record: Any) -> None:
        """保存决策日志记录（duck typing，兼容 L3 DecisionLogRecord）。

        failure_category 存 enum .value（字符串），避免 import L3 类型。
        """
        now = utc_now_iso()
        fc_value = record.failure_category.value if record.failure_category else None
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO specialist_decision_log
                       (log_id, specialist_name, task_id, goal, success_criteria,
                        failure_category, success_criteria_met, lesson_text, timestamp)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (record.log_id, record.specialist_name, record.task_id,
                     record.goal, record.success_criteria, fc_value,
                     record.success_criteria_met, record.lesson_text,
                     record.timestamp or now),
                )
                conn.commit()
            finally:
                conn.close()

    def get_decision_logs(
        self, specialist_name: str, failure_category: Any | None, limit: int,
    ) -> list[Any]:
        """查询决策日志（lazy import L3 类型避免循环依赖）。

        返回 list[DecisionLogRecord]。failure_category 为 None 时不过滤。
        """
        from poirot.backend.agents.multiagent.evolution.types import FailureCategory
        from poirot.backend.agents.multiagent.eval.types import DecisionLogRecord

        with self._lock:
            conn = self._connect()
            try:
                if failure_category is not None:
                    cursor = conn.execute(
                        """SELECT log_id, specialist_name, task_id, goal, success_criteria,
                                  failure_category, success_criteria_met, lesson_text, timestamp
                           FROM specialist_decision_log
                           WHERE specialist_name=? AND failure_category=?
                           ORDER BY timestamp DESC LIMIT ?""",
                        (specialist_name, failure_category.value, limit),
                    )
                else:
                    cursor = conn.execute(
                        """SELECT log_id, specialist_name, task_id, goal, success_criteria,
                                  failure_category, success_criteria_met, lesson_text, timestamp
                           FROM specialist_decision_log
                           WHERE specialist_name=?
                           ORDER BY timestamp DESC LIMIT ?""",
                        (specialist_name, limit),
                    )
                rows = cursor.fetchall()
            finally:
                conn.close()

        records: list[DecisionLogRecord] = []
        for row in rows:
            fc = FailureCategory(row[5]) if row[5] else None
            records.append(DecisionLogRecord(
                log_id=row[0], specialist_name=row[1], task_id=row[2],
                goal=row[3], success_criteria=row[4], failure_category=fc,
                success_criteria_met=row[6], lesson_text=row[7], timestamp=row[8],
            ))
        return records

    def archive_decision_logs(self, retention_days: int) -> int:
        """归档过期决策日志（先复制到 archive 表，再删主表）。

        返回归档条数。
        """
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        now = utc_now_iso()
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    "SELECT log_id FROM specialist_decision_log WHERE timestamp < ?",
                    (cutoff,),
                )
                expired_ids = [row[0] for row in cursor.fetchall()]
                if not expired_ids:
                    return 0
                placeholders = ",".join("?" * len(expired_ids))
                conn.execute(
                    f"""INSERT INTO specialist_decision_log_archive
                        (log_id, specialist_name, task_id, goal, success_criteria,
                         failure_category, success_criteria_met, lesson_text, timestamp, archived_at)
                        SELECT log_id, specialist_name, task_id, goal, success_criteria,
                               failure_category, success_criteria_met, lesson_text, timestamp, ?
                        FROM specialist_decision_log WHERE log_id IN ({placeholders})""",
                    [now] + expired_ids,
                )
                conn.execute(
                    f"DELETE FROM specialist_decision_log WHERE log_id IN ({placeholders})",
                    expired_ids,
                )
                conn.commit()
                return len(expired_ids)
            finally:
                conn.close()