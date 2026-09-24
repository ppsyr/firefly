"""运行管理器 — 运行（run）的创建、状态推进与持久化。

【整体职责】
管理单次运行的生命周期：创建 run（生成 RunContext + RunRecord + RunJournal）、
推进状态（running / success / failed）、更新并持久化 RunRecord 到磁盘。
内存中维护 run_id → RunRecord / RunContext 的映射。

【内容摘要】
- _CST            : 东八区时区常量，用于生成 run_id 时间戳。
- _make_run_id    : 生成 run_id（run-{时间戳}-{4位随机后缀}）。
- RunManager      : 运行管理器，含 create_run / mark_running / mark_success /
                    mark_failed / get_run 及内部持久化辅助。
- _store_record   : 更新内存记录并写盘。
- _write_record   : 把 RunRecord 序列化写入 record_path。
- _require_record : 取记录，缺失抛 KeyError。
- _require_context: 取上下文，缺失抛 KeyError。

【职责边界】
- 只负责：run 的创建、状态推进、记录持久化、内存映射维护。
- 不负责：RunRecord / RunContext 的结构定义（run_record / run_context）、
  事件日志实现（RunJournal）、配置结构（AppConfig）、实际执行业务逻辑（graph / agent）。

【INVARIANT】
- 状态单向推进：PENDING → RUNNING → SUCCESS / ERROR，均通过 replace 生成新 record。
- 记录不可变：RunRecord 为 frozen，状态更新用 dataclasses.replace 产生新实例。
- 双映射维护：_records 与 _contexts 以 run_id 为键，须同时存在。
- 持久化时机：每次状态变更（create / mark_*）都写盘 record.json。
- 时间戳统一 UTC ISO：created_at / updated_at / started_at / finished_at 均用 utc_now_iso()。
- 输出目录规则：有 thread_dir 时用 {thread_dir}/runs/{run_id}，否则用 {logs_root}/{run_id}。
- run_id 唯一：缺省由 _make_run_id 生成（东八区时间戳 + 随机后缀）。
"""
from __future__ import annotations

import json
import random
import string
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from poirot.backend.agents.config.schema import AppConfig
from poirot.backend.agents.journal.events import utc_now_iso
from poirot.backend.agents.journal.run_journal import RunJournal
from poirot.backend.agents.runtime.run_context import RunContext
from poirot.backend.agents.runtime.run_record import RunRecord, RunStatus

_CST = timezone(timedelta(hours=8))


def _make_run_id() -> str:
    """生成 run_id：run-{东八区时间戳}-{4位随机后缀}。

    Returns:
        str: 形如 "run-20240101T120000-ab3d" 的唯一标识。
    """
    ts = datetime.now(_CST).strftime("%Y%m%dT%H%M%S")
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"run-{ts}-{suffix}"


class RunManager:
    """运行管理器：创建 run、推进状态、持久化记录。

    Attributes:
        config: 应用配置。
        _records: run_id → RunRecord 映射。
        _contexts: run_id → RunContext 映射。
    """

    def __init__(self, config: AppConfig) -> None:
        """初始化。

        Args:
            config: 应用配置。
        """
        self.config = config
        self._records: dict[str, RunRecord] = {}
        self._contexts: dict[str, RunContext] = {}

    def create_run(
        self,
        thread_id: str,
        user_id: str | None = None,
        run_id: str | None = None,
        session_id: str | None = None,
        trace_id: str | None = None,
        model_name: str | None = None,
        thread_dir: Path | None = None,
    ) -> RunContext:
        """创建一次运行，初始化 RunContext / RunRecord / RunJournal 并写盘。

        Args:
            thread_id: 所属线程 ID。
            user_id: 用户标识，可选。
            run_id: 指定 run_id；缺省由 _make_run_id 生成。
            session_id: 会话 ID，可选。
            trace_id: 追踪 ID，可选。
            model_name: 模型名；缺省用配置的 researcher_model。
            thread_dir: 线程目录；非空时输出到 {thread_dir}/runs/{run_id}，
                否则输出到 {logs_root}/{run_id}。

        Returns:
            RunContext: 新建的运行上下文。
        """
        created_run_id = run_id or _make_run_id()
        if thread_dir is not None:
            output_dir = thread_dir / "runs" / created_run_id
        else:
            output_dir = Path(self.config.runtime.logs_root) / created_run_id
        journal = RunJournal(
            run_id=created_run_id,
            events_path=output_dir / "events.jsonl",
        )
        context = RunContext(
            run_id=created_run_id,
            thread_id=thread_id,
            user_id=user_id,
            session_id=session_id,
            trace_id=trace_id,
            config=self.config,
            budget={},
            output_dir=output_dir,
            enabled_middlewares=self.config.middleware.enabled,
            journal=journal,
        )
        now = utc_now_iso()
        record = RunRecord(
            run_id=created_run_id,
            thread_id=thread_id,
            user_id=user_id,
            status=RunStatus.PENDING,
            created_at=now,
            updated_at=now,
            model_name=model_name or self.config.models.researcher_model,
            metadata={"expert_mode": self.config.runtime.expert_mode},
        )
        self._contexts[created_run_id] = context
        self._records[created_run_id] = record
        self._write_record(record)
        return context

    def mark_running(self, run_id: str) -> RunRecord:
        """标记为运行中：更新状态与 started_at，写盘并记事件。

        Args:
            run_id: 运行 ID。

        Returns:
            RunRecord: 更新后的记录。
        """
        record = self._require_record(run_id)
        now = utc_now_iso()
        updated = replace(
            record,
            status=RunStatus.RUNNING,
            started_at=record.started_at or now,
            updated_at=now,
        )
        self._store_record(updated)
        self._require_context(run_id).journal.append(
            "run.started",
            {"expert_mode": self.config.runtime.expert_mode},
        )
        return updated

    def mark_success(
        self,
        run_id: str,
        usage_summary: dict[str, Any] | None = None,
    ) -> RunRecord:
        """标记为成功：更新状态、finished_at 与 token 用量，写盘并记事件。

        Args:
            run_id: 运行 ID。
            usage_summary: 用量摘要，可含 total_tokens。

        Returns:
            RunRecord: 更新后的记录。
        """
        record = self._require_record(run_id)
        now = utc_now_iso()
        usage = usage_summary or {}
        updated = replace(
            record,
            status=RunStatus.SUCCESS,
            updated_at=now,
            finished_at=now,
            total_tokens=usage.get("total_tokens", record.total_tokens),
        )
        self._store_record(updated)
        self._require_context(run_id).journal.append("run.finished", usage)
        return updated

    def mark_failed(self, run_id: str, error: str) -> RunRecord:
        """标记为失败：更新状态、finished_at 与 error，写盘并记事件。

        Args:
            run_id: 运行 ID。
            error: 错误信息。

        Returns:
            RunRecord: 更新后的记录。
        """
        record = self._require_record(run_id)
        now = utc_now_iso()
        updated = replace(
            record,
            status=RunStatus.ERROR,
            updated_at=now,
            finished_at=now,
            error=error,
        )
        self._store_record(updated)
        self._require_context(run_id).journal.append("run.failed", {"error": error})
        return updated

    def get_run(self, run_id: str) -> RunRecord | None:
        """按 run_id 取记录；不存在返回 None。"""
        return self._records.get(run_id)

    def _store_record(self, record: RunRecord) -> None:
        """更新内存记录并写盘。

        Args:
            record: 待存储的记录。
        """
        self._records[record.run_id] = record
        self._write_record(record)

    def _write_record(self, record: RunRecord) -> None:
        """把 RunRecord 序列化写入 context.record_path。

        Args:
            record: 待写入的记录。
        """
        context = self._require_context(record.run_id)
        context.output_dir.mkdir(parents=True, exist_ok=True)
        context.record_path.write_text(
            json.dumps(record.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _require_record(self, run_id: str) -> RunRecord:
        """取记录；缺失抛 KeyError。

        Args:
            run_id: 运行 ID。

        Returns:
            RunRecord: 对应记录。

        Raises:
            KeyError: run_id 不存在时。
        """
        try:
            return self._records[run_id]
        except KeyError as exc:
            raise KeyError(f"Unknown run_id: {run_id}") from exc

    def _require_context(self, run_id: str) -> RunContext:
        """取上下文；缺失抛 KeyError。

        Args:
            run_id: 运行 ID。

        Returns:
            RunContext: 对应上下文。

        Raises:
            KeyError: run_id 不存在时。
        """
        try:
            return self._contexts[run_id]
        except KeyError as exc:
            raise KeyError(f"Unknown run_id: {run_id}") from exc