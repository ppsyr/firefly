"""BudgetGuard — per-specialist budget 三维度记账 + per-day UTC 0 重置 + 超限 fallback lead。

【整体职责】
为每个 specialist 做每日预算的记账与超限检查：三维度（tokens / cost_usd / calls）
累加当日用量，超限时返回 allowed=False 并指定 fallback 目标为 lead；
80% 用量时写预警记录。全部持久化到 multiagent.db。

【内容摘要】
- _BUDGET_SCHEMA_SQL           : 两张表建表 SQL（specialist_budget_usage / budget_warnings）。
- BudgetLimit(frozen)          : per-specialist 单日预算上限。
- _utc_date_str()              : UTC 日期字符串（per-day 重置 key）。
- BudgetGuard                  : 记账主类（check_and_record / get_today_usage / fallback_target / get_warnings）。

【职责边界】
- 只负责：三维度记账、超限检查、80% 预警写库、按天查询。
- 不负责：成本计算（CostRecord 由调用方构造）、主动通知 LLM（不推送，只写库）、
  在 system prompt 中注入（通过 tool 返 JSON 通知）。
- 不持有运行时状态：只持有 db_path / limits / warning_threshold + 锁。

【INVARIANT】
- 三维度记账：token + cost_usd + 调用次数；cost_usd 为主触发维度。
- per-day UTC 0 点重置：日期 key 用 UTC 日期字符串。
- 超限 fallback 固定 "lead"：不 fallback 到另一个 specialist。
- 超限通知方式：通过 tool 返回 BudgetExceeded JSON，不污染 system prompt。
- 80% 预警只写 metrics 表，不主动通知 LLM。
- 持久化到 multiagent.db 的 specialist_budget_usage + budget_warnings 表。
- 超限检查优先级：cost_usd > tokens > calls。
- 80% 预警只在"跨过阈值且未超限"时写一次（old_pct < threshold <= new_pct）。
- 无记录时 get_today_usage 返回全 0。
- WAL 模式 + busy_timeout=30000 + threading.Lock 保护。
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from poirot.backend.agents.journal.events import utc_now_iso
from poirot.backend.agents.multiagent.evolution.types import (
    BudgetCheckResult,
    BudgetRemaining,
    CostRecord,
)

# budget 两张表建表 SQL（幂等）。
_BUDGET_SCHEMA_SQL = """
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
    warning_id     TEXT PRIMARY KEY,
    specialist_name TEXT NOT NULL,
    date           TEXT NOT NULL,
    warning_type   TEXT NOT NULL,
    detail         TEXT,
    timestamp      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_budget_warnings_name_date ON budget_warnings(specialist_name, date);
"""


@dataclass(frozen=True)
class BudgetLimit:
    """per-specialist 单日预算上限。

    默认值：per_day_tokens=200000 / per_day_cost_usd=$20 / per_day_calls=50。
    """

    per_day_tokens: int = 200000
    per_day_cost_usd: float = 20.0
    per_day_calls: int = 50


def _utc_date_str() -> str:
    """UTC 日期字符串 'YYYY-MM-DD'（per-day 重置 key）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class BudgetGuard:
    """per-specialist 预算三维度记账 + per-day 重置 + 超限 fallback lead。

    三维度记账（token + cost_usd + 调用次数），cost_usd 为主触发维度；
    per-day UTC 0 点重置；超限 fallback 到 lead；80% 预警写库不推送。
    """

    def __init__(
        self,
        db_path: str = ".poirot/multiagent.db",
        limits: dict[str, BudgetLimit] | None = None,
        warning_threshold: float = 0.8,
    ) -> None:
        """初始化。

        Args:
            db_path: SQLite 路径，默认 .poirot/multiagent.db。
            limits: per-specialist 预算上限映射；缺省时用 BudgetLimit() 默认值。
            warning_threshold: 预警阈值（默认 0.8，即 80%）。
        """
        self._db_path = db_path
        self._limits = limits or {}
        self._warning_threshold = warning_threshold
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
                conn.executescript(_BUDGET_SCHEMA_SQL)
                conn.commit()
            finally:
                conn.close()

    def _get_limit(self, specialist_name: str) -> BudgetLimit:
        """取 specialist 的预算上限；无配置时用默认 BudgetLimit()。"""
        return self._limits.get(specialist_name, BudgetLimit())

    def check_and_record(
        self,
        specialist_name: str,
        cost: CostRecord,
    ) -> BudgetCheckResult:
        """三维度记账 + 超限检查 + 80% 预警。

        流程：
        1. 读当日已用量（无记录则为 0）。
        2. 累加本次用量。
        3. UPSERT 写回 specialist_budget_usage。
        4. 检查超限：cost_usd > tokens > calls 优先级。
        5. 跨过 80% 阈值且未超限时写 budget_warnings。
        6. 返回 BudgetCheckResult（allowed / reason / remaining / fallback_target）。

        Args:
            specialist_name: specialist 名。
            cost: 本次用量（tokens / cost_usd / calls）。

        Returns:
            BudgetCheckResult；超限时 allowed=False 且 reason 非空。
        """
        limit = self._get_limit(specialist_name)
        date_str = _utc_date_str()
        now = utc_now_iso()

        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """SELECT tokens_used, cost_usd_used, calls_used
                        FROM specialist_budget_usage
                        WHERE specialist_name=? AND date=?""",
                    (specialist_name, date_str),
                ).fetchone()
                current_tokens = row[0] if row else 0
                current_cost = row[1] if row else 0.0
                current_calls = row[2] if row else 0

                # 累加后用量
                new_tokens = current_tokens + cost.tokens
                new_cost = current_cost + cost.cost_usd
                new_calls = current_calls + cost.calls

                # UPSERT 写入用量
                conn.execute(
                    """INSERT INTO specialist_budget_usage
                        (specialist_name, date, tokens_used, cost_usd_used,
                         calls_used, last_updated)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(specialist_name, date) DO UPDATE SET
                            tokens_used=excluded.tokens_used,
                            cost_usd_used=excluded.cost_usd_used,
                            calls_used=excluded.calls_used,
                            last_updated=excluded.last_updated""",
                    (specialist_name, date_str, new_tokens, new_cost, new_calls, now),
                )

                # 检查超限（cost_usd 主触发）
                reason: str | None = None
                if new_cost > limit.per_day_cost_usd:
                    reason = "daily_cost_exceeded"
                elif new_tokens > limit.per_day_tokens:
                    reason = "daily_tokens_exceeded"
                elif new_calls > limit.per_day_calls:
                    reason = "daily_calls_exceeded"

                # 80% 预警（cost_usd 主维度，仅跨阈值且未超限时写）
                old_pct = current_cost / limit.per_day_cost_usd if limit.per_day_cost_usd > 0 else 0.0
                new_pct = new_cost / limit.per_day_cost_usd if limit.per_day_cost_usd > 0 else 0.0
                if old_pct < self._warning_threshold <= new_pct and reason is None:
                    warning_id = f"warn_{specialist_name}_{date_str}_{int(new_pct * 100)}"
                    conn.execute(
                        """INSERT INTO budget_warnings
                            (warning_id, specialist_name, date, warning_type, detail, timestamp)
                            VALUES (?, ?, ?, ?, ?, ?)""",
                        (warning_id, specialist_name, date_str,
                         "approaching_80_percent",
                         f"cost_usd {new_pct:.0%} of limit", now),
                    )

                conn.commit()
            finally:
                conn.close()

        if reason is not None:
            return BudgetCheckResult(
                allowed=False,
                specialist_name=specialist_name,
                reason=reason,
                remaining=BudgetRemaining(
                    tokens=max(0, limit.per_day_tokens - new_tokens),
                    cost_usd=max(0.0, limit.per_day_cost_usd - new_cost),
                    calls=max(0, limit.per_day_calls - new_calls),
                ),
                fallback_target="lead",
            )

        return BudgetCheckResult(
            allowed=True,
            specialist_name=specialist_name,
            reason=None,
            remaining=BudgetRemaining(
                tokens=max(0, limit.per_day_tokens - new_tokens),
                cost_usd=max(0.0, limit.per_day_cost_usd - new_cost),
                calls=max(0, limit.per_day_calls - new_calls),
            ),
            fallback_target="lead",
        )

    def get_today_usage(self, specialist_name: str) -> dict[str, Any]:
        """查询当日用量（per-day UTC 0 点重置）。

        Args:
            specialist_name: specialist 名。

        Returns:
            当日用量 dict（tokens_used / cost_usd_used / calls_used）；无记录返全 0。
        """
        date_str = _utc_date_str()
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """SELECT tokens_used, cost_usd_used, calls_used
                        FROM specialist_budget_usage
                        WHERE specialist_name=? AND date=?""",
                    (specialist_name, date_str),
                ).fetchone()
            finally:
                conn.close()
        if row is None:
            return {"tokens_used": 0, "cost_usd_used": 0.0, "calls_used": 0}
        return {
            "tokens_used": row[0],
            "cost_usd_used": row[1],
            "calls_used": row[2],
        }

    def fallback_target(self, specialist_name: str) -> str:
        """超限时的 fallback 目标；固定返回 "lead"（不 fallback 另一 specialist）。"""
        return "lead"

    def get_warnings(self, specialist_name: str, date_str: str | None = None) -> list[dict]:
        """查询 80% 预警记录（CLI inspect 用，无主动推送）。

        Args:
            specialist_name: specialist 名。
            date_str: 日期字符串；None 表示当天。

        Returns:
            预警记录 dict 列表（warning_id / warning_type / detail / timestamp）。
        """
        date_str = date_str or _utc_date_str()
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """SELECT warning_id, warning_type, detail, timestamp
                        FROM budget_warnings
                        WHERE specialist_name=? AND date=?
                        ORDER BY timestamp DESC""",
                    (specialist_name, date_str),
                ).fetchall()
            finally:
                conn.close()
        return [
            {
                "warning_id": r[0],
                "warning_type": r[1],
                "detail": r[2] or "",
                "timestamp": r[3],
            }
            for r in rows
        ]