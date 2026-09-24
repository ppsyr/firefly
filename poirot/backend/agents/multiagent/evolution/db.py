"""L2 db schema management — 6 张表 + PRAGMA user_version v1→v2。

【整体职责】
管理 L2 进化层所需的 SQLite schema：创建 6 张表（evolution_artifacts /
evolution_experiments / l2_metrics / specialist_budget_usage / budget_warnings /
l2_blocked_patterns），并把 PRAGMA user_version 从 v1（L1）升级到 v2（L2）。
与 L1 共用同一个 metrics 数据库（不同表），不破坏已有表。

【内容摘要】
- _L2_SCHEMA_SQL / _L2_SCHEMA_VERSION : 建表 SQL（6 表 + 索引）+ schema 版本号。
- L2SchemaManager                     : schema 管理主类（__init__ 幂等建表）。
- schema_version (property)           : 暴露当前 schema 版本号。

【职责边界】
- 只负责：L2 六张表的建表、版本号升级、幂等性保证。
- 不负责：表的读写（各业务模块负责）、L1/L3 表的建表（各自负责）。
- 不持有运行时状态：只持有 db_path + 锁。

【INVARIANT】
- 与 L1 共用同一个 DB 文件（默认 .poirot/multiagent.db），但表不同——Z3 模式。
- 幂等：重复对 v2 数据库运行是 no-op（executescript 用 IF NOT EXISTS）。
- 不破坏已有表：只 CREATE IF NOT EXISTS L2 表，不 DROP / ALTER L1 表。
- PRAGMA user_version 只在 version < 2 时更新为 2（不降级）。
- WAL 模式 + busy_timeout=30000 + threading.Lock 保护初始化。
- 建表时自动创建父目录。
- evolution_artifacts 有 UNIQUE(artifact_type, template_id, version) 约束。
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

# L2 建表 SQL：6 张表 + 索引。全部 IF NOT EXISTS，可重复执行。
_L2_SCHEMA_SQL = """
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
"""

# 当前 schema 版本号（L1 为 1，L2 升到 2）。
_L2_SCHEMA_VERSION = 2


class L2SchemaManager:
    """L2 SQLite schema 管理器（6 张表 + PRAGMA user_version v1→v2）。

    - 幂等：对已有 v2 数据库重复运行是 no-op。
    - 不破坏 L1 表：specialist_records / specialist_judgments 保持原样。
    """

    def __init__(self, db_path: str = ".poirot/multiagent.db") -> None:
        """初始化：建父目录 + 幂等建表 / 升级版本号。"""
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
        """幂等建表 + 版本号升级。

        - 执行 _L2_SCHEMA_SQL（IF NOT EXISTS，可重复）。
        - 仅当 version < _L2_SCHEMA_VERSION 时更新 user_version。
        """
        with self._lock:
            conn = self._connect()
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                # 始终执行 CREATE IF NOT EXISTS（幂等）
                conn.executescript(_L2_SCHEMA_SQL)
                # 仅在低于 L2 版本时升级 PRAGMA user_version v1 -> v2
                if version < _L2_SCHEMA_VERSION:
                    conn.execute(f"PRAGMA user_version = {_L2_SCHEMA_VERSION}")
                conn.commit()
            finally:
                conn.close()

    @property
    def schema_version(self) -> int:
        """暴露当前 schema 版本号（固定为 _L2_SCHEMA_VERSION）。"""
        return _L2_SCHEMA_VERSION