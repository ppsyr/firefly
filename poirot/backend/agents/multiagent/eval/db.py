"""L3 db schema management — 2 张表 + PRAGMA user_version v2→v3。

【整体职责】
管理 L3 评估层所需的 SQLite schema：创建 specialist_decision_log 与
specialist_decision_log_archive 两张表，并把 PRAGMA user_version 从 v2（L2）
升级到 v3（L3）。与 L1/L2 共用同一个 metrics 数据库（不同表），不破坏已有表。

【内容摘要】
- _L3_SCHEMA_SQL / _L3_SCHEMA_VERSION : 建表 SQL（2 表 + 索引）+ schema 版本号。
- L3SchemaManager                     : schema 管理主类（__init__ 幂等建表）。
- schema_version (property)           : 暴露当前 schema 版本号。

【职责边界】
- 只负责：L3 两张表的建表、版本号升级、幂等性保证。
- 不负责：表的读写（metrics.py 负责）、L1/L2 表的建表（各自负责）、
  归档逻辑（archive_decision_logs 在 metrics.py 里）。
- 不持有运行时状态：只持有 db_path + 锁。

【INVARIANT】
- 与 L1/L2 共用同一个 DB 文件（默认 .poirot/multiagent.db），但表不同——Z3 模式。
- 幂等：重复对 v3 数据库运行是 no-op（executescript 用 IF NOT EXISTS）。
- 不破坏已有表：只 CREATE IF NOT EXISTS L3 表，不 DROP / ALTER L1/L2 表。
- PRAGMA user_version 只在 version < 3 时更新为 3（不降级）。
- WAL 模式 + busy_timeout=30000 + threading.Lock 保护初始化。
- 归档语义：90 天后移到 archive 表，不删除（archive_decision_logs 负责）。
- 建表时自动创建父目录。
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

# L3 建表 SQL：2 张表 + 索引。全部 IF NOT EXISTS，可重复执行。
_L3_SCHEMA_SQL = """
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

# 当前 schema 版本号（L2 为 2，L3 升到 3）。
_L3_SCHEMA_VERSION = 3


class L3SchemaManager:
    """L3 SQLite schema 管理器（2 张表 + PRAGMA user_version v2→v3）。

    - 幂等：对已有 v3 数据库重复运行是 no-op。
    - 不破坏 L1/L2 表：specialist_records / evolution_artifacts / l2_metrics 保持原样。
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

        - 执行 _L3_SCHEMA_SQL（IF NOT EXISTS，可重复）。
        - 仅当 version < _L3_SCHEMA_VERSION 时更新 user_version。
        """
        with self._lock:
            conn = self._connect()
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                conn.executescript(_L3_SCHEMA_SQL)
                if version < _L3_SCHEMA_VERSION:
                    conn.execute(f"PRAGMA user_version = {_L3_SCHEMA_VERSION}")
                conn.commit()
            finally:
                conn.close()

    @property
    def schema_version(self) -> int:
        """暴露当前 schema 版本号（固定为 _L3_SCHEMA_VERSION）。"""
        return _L3_SCHEMA_VERSION