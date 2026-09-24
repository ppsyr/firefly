"""VersionDAG — 演化产物 version DAG + is_active 单指针 + 回滚 + hash 防环。

【整体职责】
持久化 L2 演化产物与实验记录：commit 写入 evolution_artifacts +
evolution_experiments 两张表，accept 时更新 is_active 单指针；
提供 get_active / get_history / rollback / hash_exists_in_recent 四个查询。

【内容摘要】
- _VERSION_DAG_SCHEMA_SQL : 两张表建表 SQL（幂等）。
- ArtifactRow(frozen)     : evolution_artifacts 表行。
- _artifact_type_name()   : 从 artifact 实例推断 artifact_type 名。
- _serialize_payload()    : 序列化 artifact 为 JSON。
- _deserialize_payload()  : 从 ArtifactRow 反序列化 artifact。
- _DummySelector          : 反序列化 placeholder。
- VersionDAG              : 主类（commit / get_active / get_history / rollback / hash_exists_in_recent）。

【职责边界】
- 只负责：持久化 artifact + experiment、is_active 单指针维护、回滚、hash 防环查询。
- 不负责：变异（mutator 负责）、评估（PromotionGate 负责）、
  晋升决策（PromotionGate.decide 负责）。
- 不持有重状态：只持有 db_path + 锁。

【INVARIANT】
- 持久化 SQLite（multiagent.db 加表，与 L1 metrics 同 db 不同表——Z3 模式）。
- is_active 单指针：同 artifact_type + template_id 仅 1 行 is_active=1。
- get_active 每次查 DB（不缓存，保 hot swap）。
- reject candidate 也存（防重复尝试），但 is_active 不更新。
- hash_exists_in_recent 检查近 5 版防环。
- 演化失败保持旧 is_active（不更新指针）。
- commit 同时写 artifact 和 experiment 记录。
- artifact_id 格式：art_<12 位 hex>；experiment_id 格式：exp_<12 位 hex>。
- extractors / filters / skill_selector 仅存类名，反序列化为空 tuple / DummySelector
  （L1 hot swap 时重新构造）。
- WAL 模式 + busy_timeout=30000 + threading.Lock。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from poirot.backend.agents.journal.events import utc_now_iso
from poirot.backend.agents.multiagent.evolution.types import (
    ContextSummaryTemplate,
    EvolutionArtifact,
    SkillInjectionTemplate,
)

# L2 表 schema（加到 multiagent.db，与 L1 specialist_records 同 db 不同表）。
_VERSION_DAG_SCHEMA_SQL = """
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
    artifact_id     TEXT NOT NULL REFERENCES evolution_artifacts(artifact_id),
    from_artifact_id TEXT,
    trigger_source  TEXT NOT NULL,
    trigger_detail  TEXT,
    eval_method     TEXT,
    eval_result_json TEXT,
    decision        TEXT NOT NULL,
    timestamp       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evo_exp_artifact ON evolution_experiments(artifact_id);
"""


@dataclass(frozen=True)
class ArtifactRow:
    """evolution_artifacts 表行（get_active / get_history 返回）。

    字段与表列一一对应；is_active 从 INTEGER 转 bool。
    """

    artifact_id: str
    artifact_type: str
    version: str
    template_id: str
    payload_json: str
    artifact_hash: str
    rationale: str
    created_at: str
    is_active: bool


def _artifact_type_name(artifact: EvolutionArtifact) -> str:
    """从 artifact 实例推断 artifact_type 名（'context_summary' | 'skill_injection'）。"""
    if isinstance(artifact, ContextSummaryTemplate):
        return "context_summary"
    if isinstance(artifact, SkillInjectionTemplate):
        return "skill_injection"
    return type(artifact).__name__.lower()


def _serialize_payload(artifact: EvolutionArtifact) -> str:
    """序列化 artifact payload 为 JSON（结构化 dataclass，可 diff / 回滚）。"""
    if isinstance(artifact, ContextSummaryTemplate):
        return json.dumps({
            "version": artifact.version,
            "template_id": artifact.template_id,
            "extractors": [type(e).__name__ for e in artifact.extractors],
            "filters": [type(f).__name__ for f in artifact.filters],
            "max_tokens": artifact.max_tokens,
            "prompt_skeleton": artifact.prompt_skeleton,
        }, sort_keys=True)
    if isinstance(artifact, SkillInjectionTemplate):
        return json.dumps({
            "version": artifact.version,
            "template_id": artifact.template_id,
            "skill_selector": type(artifact.skill_selector).__name__,
            "injection_format": artifact.injection_format,
            "max_skills": artifact.max_skills,
        }, sort_keys=True)
    return json.dumps({"version": artifact.version, "template_id": artifact.template_id})


def _deserialize_payload(row: ArtifactRow) -> EvolutionArtifact:
    """从 payload_json 反序列化 artifact。

    extractors / filters / skill_selector 仅存类名，反序列化为空 tuple / DummySelector
    （hot swap 时 L1 重新构造真实对象）。
    """
    payload = json.loads(row.payload_json)
    if row.artifact_type == "context_summary":
        return ContextSummaryTemplate(
            version=payload["version"],
            template_id=payload["template_id"],
            extractors=(),
            filters=(),
            max_tokens=payload.get("max_tokens", 2000),
            prompt_skeleton=payload.get("prompt_skeleton", ""),
        )
    if row.artifact_type == "skill_injection":
        return SkillInjectionTemplate(
            version=payload["version"],
            template_id=payload["template_id"],
            skill_selector=_DummySelector(),  # placeholder，L1 重新构造
            injection_format=payload.get("injection_format", ""),
            max_skills=payload.get("max_skills", 3),
        )
    raise ValueError(f"unknown artifact_type: {row.artifact_type}")


class _DummySelector:
    """反序列化 placeholder（L1 hot swap 时重新构造真实 selector）。"""

    def select(self, goal: str, available_skills: list) -> tuple:
        return ()


class VersionDAG:
    """演化产物 version DAG + is_active 单指针 + 回滚 + hash 防环。

    - 持久化 SQLite（multiagent.db 加表，与 L1 metrics 同 db 不同表）。
    - is_active 单指针：同 template_id 仅 1 行 is_active=1。
    - get_active 每次查 DB（不缓存，保 hot swap）。
    - reject candidate 也存（防重复尝试）。
    - hash_exists_in_recent 近 5 版防环。
    - 演化失败保持旧 is_active。
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
                conn.executescript(_VERSION_DAG_SCHEMA_SQL)
                conn.commit()
            finally:
                conn.close()

    def commit(
        self,
        artifact: EvolutionArtifact,
        eval_result: Any,
        *,
        from_artifact_id: str | None = None,
        trigger_source: str = "manual",
        trigger_detail: str = "",
        rationale: str = "",
        decision: str = "accept",
    ) -> str:
        """commit artifact + eval_result 到 DB + is_active 单指针更新（accept 时）。

        - 插入 evolution_artifacts 行（is_active 初始 0）。
        - decision == "accept" → 先把同类型同 template_id 全置 0，再把本行置 1。
        - 插入 evolution_experiments 记录（含 eval_result_json）。
        - reject candidate 也存（防重复尝试），但 is_active 不更新。

        Args:
            artifact: 演化产物。
            eval_result: 评估结果（任意对象，取 candidate_score 等字段）。
            from_artifact_id: 父版本 ID（可选）。
            trigger_source: 触发源（写 experiment）。
            trigger_detail: 触发详情。
            rationale: 演化理由。
            decision: "accept" 才更新 is_active。

        Returns:
            新 artifact_id。
        """
        artifact_id = f"art_{uuid.uuid4().hex[:12]}"
        artifact_type = _artifact_type_name(artifact)
        payload_json = _serialize_payload(artifact)
        artifact_hash = artifact.artifact_hash
        now = utc_now_iso()

        with self._lock:
            conn = self._connect()
            try:
                # 插入 artifact
                conn.execute(
                    """INSERT INTO evolution_artifacts
                        (artifact_id, artifact_type, version, template_id,
                         payload_json, artifact_hash, rationale, created_at, is_active)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (artifact_id, artifact_type, artifact.version,
                     artifact.template_id, payload_json, artifact_hash,
                     rationale, now, 0),
                )
                # accept 时 is_active 单指针更新
                if decision == "accept":
                    conn.execute(
                        "UPDATE evolution_artifacts SET is_active=0 WHERE artifact_type=? AND template_id=?",
                        (artifact_type, artifact.template_id),
                    )
                    conn.execute(
                        "UPDATE evolution_artifacts SET is_active=1 WHERE artifact_id=?",
                        (artifact_id,),
                    )
                # 插入 experiment 记录
                exp_id = f"exp_{uuid.uuid4().hex[:12]}"
                eval_json = json.dumps({
                    "candidate_score": getattr(eval_result, "candidate_score", 0.0),
                    "baseline_score": getattr(eval_result, "baseline_score", 0.0),
                    "ci_low": getattr(eval_result, "ci_low", 0.0),
                    "ci_high": getattr(eval_result, "ci_high", 0.0),
                    "sample_size": getattr(eval_result, "sample_size", 0),
                    "success": getattr(eval_result, "success", True),
                }) if eval_result else "{}"
                conn.execute(
                    """INSERT INTO evolution_experiments
                        (experiment_id, artifact_id, from_artifact_id,
                         trigger_source, trigger_detail, eval_method,
                         eval_result_json, decision, timestamp)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (exp_id, artifact_id, from_artifact_id,
                     trigger_source, trigger_detail, "longitudinal_pairs",
                     eval_json, decision, now),
                )
                conn.commit()
            finally:
                conn.close()
        return artifact_id

    def get_active(self, artifact_type: type) -> EvolutionArtifact | None:
        """L1 每次调用查 DB 取 is_active（不缓存，保 hot swap）。

        按 artifact_type 查 is_active=1 的最新一条；无则返回 None。

        Args:
            artifact_type: ContextSummaryTemplate 或 SkillInjectionTemplate 类。

        Returns:
            反序列化后的 artifact，或 None。
        """
        type_name = "context_summary" if artifact_type is ContextSummaryTemplate else "skill_injection"
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """SELECT artifact_id, artifact_type, version, template_id,
                              payload_json, artifact_hash, rationale, created_at, is_active
                        FROM evolution_artifacts
                        WHERE artifact_type=? AND is_active=1
                        ORDER BY created_at DESC LIMIT 1""",
                    (type_name,),
                ).fetchone()
            finally:
                conn.close()
        if row is None:
            return None
        art_row = ArtifactRow(
            artifact_id=row[0], artifact_type=row[1], version=row[2],
            template_id=row[3], payload_json=row[4], artifact_hash=row[5],
            rationale=row[6] or "", created_at=row[7], is_active=bool(row[8]),
        )
        return _deserialize_payload(art_row)

    def get_history(
        self, artifact_type: type, template_id: str
    ) -> list[ArtifactRow]:
        """查询同 template_id 的所有版本（按 created_at 降序）。

        Args:
            artifact_type: ContextSummaryTemplate 或 SkillInjectionTemplate 类。
            template_id: 模板 ID。

        Returns:
            ArtifactRow 列表（按 created_at 降序）。
        """
        type_name = "context_summary" if artifact_type is ContextSummaryTemplate else "skill_injection"
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """SELECT artifact_id, artifact_type, version, template_id,
                              payload_json, artifact_hash, rationale, created_at, is_active
                        FROM evolution_artifacts
                        WHERE artifact_type=? AND template_id=?
                        ORDER BY created_at DESC""",
                    (type_name, template_id),
                ).fetchall()
            finally:
                conn.close()
        return [
            ArtifactRow(
                artifact_id=r[0], artifact_type=r[1], version=r[2],
                template_id=r[3], payload_json=r[4], artifact_hash=r[5],
                rationale=r[6] or "", created_at=r[7], is_active=bool(r[8]),
            )
            for r in rows
        ]

    def rollback(self, artifact_id: str) -> None:
        """is_active 指针回退到指定 artifact_id。

        流程：
        1. 查目标 artifact 的 artifact_type + template_id。
        2. 同 artifact_type + template_id 全部 is_active=0。
        3. 指定 artifact is_active=1。

        Args:
            artifact_id: 要回滚到的版本 ID。
        """
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT artifact_type, template_id FROM evolution_artifacts WHERE artifact_id=?",
                    (artifact_id,),
                ).fetchone()
                if row is None:
                    return
                artifact_type, template_id = row[0], row[1]
                # 同类型同 template_id 全部 is_active=0
                conn.execute(
                    "UPDATE evolution_artifacts SET is_active=0 WHERE artifact_type=? AND template_id=?",
                    (artifact_type, template_id),
                )
                # 指定 artifact is_active=1
                conn.execute(
                    "UPDATE evolution_artifacts SET is_active=1 WHERE artifact_id=?",
                    (artifact_id,),
                )
                conn.commit()
            finally:
                conn.close()

    def hash_exists_in_recent(
        self, artifact_hash: str, window: int = 5
    ) -> bool:
        """检查近 N 版是否含此 hash（防环）。

        Args:
            artifact_hash: 待检查的 hash。
            window: 检查最近多少版，默认 5。

        Returns:
            是否存在。
        """
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """SELECT artifact_hash FROM evolution_artifacts
                        ORDER BY created_at DESC LIMIT ?""",
                    (window,),
                ).fetchall()
            finally:
                conn.close()
        return any(r[0] == artifact_hash for r in rows)