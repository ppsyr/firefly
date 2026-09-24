"""运行记录 — 单次运行的生命周期数据契约（frozen dataclass）。

【整体职责】
定义单次运行（run）的完整状态快照：标识（run_id / thread_id / user_id）、
生命周期状态（status + 各时间戳）、模型与 token 用量、错误信息、扩展元数据。
供 run_manager / checkpointer / 报告与观测模块共同引用。

【内容摘要】
- RunStatus : 运行状态枚举（pending / running / success / error / timeout / cancelled / interrupted）。
- RunRecord : 单次运行的记录，frozen dataclass，含 to_dict 序列化。

【职责边界】
- 只负责：定义运行记录的数据结构、字段语义、状态枚举与序列化。
- 不负责：运行的创建 / 推进 / 终结调度（run_manager）、state 持久化（checkpointer）、
  运行上下文的传递（run_context）。

【INVARIANT】
- frozen：构造后不可变，可跨组件 / 跨线程安全共享。
- 必填字段：run_id / thread_id / status / created_at / updated_at（无默认值）。
- 可选字段用 None 表达：user_id / started_at / finished_at / model_name / error / total_tokens。
- metadata 用 default_factory：避免可变默认值陷阱。
- 时间戳为字符串：created_at / updated_at / started_at / finished_at 均为 str。
- to_dict 序列化时 status 转为其字符串值（枚举 → value）。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class RunStatus(Enum):
    """运行状态枚举。

    Attributes:
        PENDING: 待启动。
        RUNNING: 运行中。
        SUCCESS: 成功结束。
        ERROR: 出错结束。
        TIMEOUT: 超时结束。
        CANCELLED: 被取消。
        INTERRUPTED: 被中断。
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class RunRecord:
    """单次运行的记录。

    Attributes:
        run_id: 运行唯一标识。
        thread_id: 所属线程 ID。
        user_id: 用户标识，可选。
        status: 运行状态。
        created_at: 创建时间。
        updated_at: 最后更新时间。
        started_at: 开始执行时间，可选。
        finished_at: 结束时间，可选。
        model_name: 使用的模型名，可选。
        error: 错误信息，仅失败时填。
        total_tokens: 总 token 用量，可选。
        metadata: 扩展元数据，默认空 dict。
    """

    run_id: str
    thread_id: str
    user_id: str | None
    status: RunStatus
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    model_name: str | None = None
    error: str | None = None
    total_tokens: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict，status 转为字符串值。

        Returns:
            dict[str, Any]: 可直接持久化 / 传输的字典。
        """
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload