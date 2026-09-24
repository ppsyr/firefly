"""运行上下文 — 单次运行的共享上下文容器（frozen dataclass）。

【整体职责】
定义单次运行（run）期间在各组件间共享的上下文：标识（run_id / thread_id /
user_id / session_id / trace_id）、配置、预算、输出目录、启用的中间件与运行日志。
并派生出记录 / 事件 / 产物三类路径，供 run_manager / journal / 中间件消费。

【内容摘要】
- RunContext        : 运行上下文，frozen dataclass，含标识、配置、预算、输出与日志。
- RunContext.record_path    : record.json 路径。
- RunContext.events_path    : events.jsonl 路径。
- RunContext.artifacts_dir  : artifacts 目录路径。

【职责边界】
- 只负责：承载运行期共享数据、派生路径。
- 不负责：上下文的创建与生命周期管理（run_manager）、运行状态推进（run_manager）、
  日志写入实现（RunJournal）、配置结构定义（AppConfig）。

【INVARIANT】
- frozen：构造后不可变，可跨组件 / 跨线程安全共享。
- 必填字段：全部字段均无默认值，构造时须显式提供。
- 路径派生自 output_dir：record_path / events_path / artifacts_dir 均为 output_dir 下的固定相对路径。
- 预算为可变 dict：budget 由运行期填充（frozen 只保证字段引用不变，不保证其内容不可变）。
- 不持有执行依赖：不含 leader_agent / capability_registry 等执行对象（由上层 AppRuntime 持有）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from poirot.backend.agents.config.schema import AppConfig
from poirot.backend.agents.journal.run_journal import RunJournal


@dataclass(frozen=True)
class RunContext:
    """单次运行的共享上下文。

    Attributes:
        run_id: 运行唯一标识。
        thread_id: 所属线程 ID。
        user_id: 用户标识，可选。
        session_id: 会话 ID，可选。
        trace_id: 追踪 ID，可选。
        config: 应用配置。
        budget: 运行期预算（可变 dict，由运行期填充）。
        output_dir: 输出目录。
        enabled_middlewares: 启用的中间件名元组。
        journal: 运行日志（事件流写入器）。
    """

    run_id: str
    thread_id: str
    user_id: str | None
    session_id: str | None
    trace_id: str | None
    config: AppConfig
    budget: dict[str, Any]
    output_dir: Path
    enabled_middlewares: tuple[str, ...]
    journal: RunJournal

    @property
    def record_path(self) -> Path:
        """record.json 路径（output_dir 下）。"""
        return self.output_dir / "record.json"

    @property
    def events_path(self) -> Path:
        """events.jsonl 路径（output_dir 下）。"""
        return self.output_dir / "events.jsonl"

    @property
    def artifacts_dir(self) -> Path:
        """artifacts 目录路径（output_dir 下）。"""
        return self.output_dir / "artifacts"