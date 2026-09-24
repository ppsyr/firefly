"""Multi-Agent 异常层次 — SpecialistError + SubagentError 两条独立层次。

【整体职责】
定义 multiagent 层可能抛出的全部异常，按两条独立层次组织：
- SpecialistError 层次：外部异构 specialist（claude / codex / pi）失败时抛。
- SubagentError 层次：Poirot 内部自复制 subagent 失败时抛。
供 runtime / registry / middleware / summarizer 抛出与捕获，保证失败语义统一。

【内容摘要】
- SpecialistError            : specialist 异常基类，带 details dict，__str__ 自动展开。
- SpecialistTimeoutError     : specialist 超时。
- SpecialistCrashError       : specialist 子进程异常退出（含 exit_code）。
- SpecialistStartupError     : specialist 启动失败（握手 / spawn 失败）。
- SpecialistCredentialError  : specialist 凭证缺失或失效。
- SpecialistNotFoundError    : specialist 未注册（Registry 查不到）。
- SubagentError              : subagent 异常基类（独立层次）。
- SubagentTimeoutError       : subagent 超时。
- SubagentMaxStepsError      : subagent 超过 max_steps 限制（含 max_steps）。

【职责边界】
- 只负责：定义异常类型、details 语义、__str__ 展开格式。
- 不负责：异常的抛出时机（runtime / registry 负责）、异常到 ToolMessage 的转换
  （OrchestrationMiddleware 负责）、异常重试策略。
- 不持有状态：异常对象只在抛出瞬间携带 details。

【INVARIANT】
- 两条层次独立：SubagentError 不继承 SpecialistError（subagent 是 Poirot 内部，非黑盒 specialist）。
- details 展开格式统一：``message (k='v', k2='v2')``；无 details 时只返回 message。
- 可选信息用"有则记"模式：timeout_seconds / exit_code / max_steps 仅在传入时才写入 details。
- SpecialistNotFoundError 由 Registry.get 缺失时抛出，details 固定带 name。
- pairing 完整性：specialist 失败一律抛 SpecialistError 子类，
  由 OrchestrationMiddleware 转为 error ToolMessage 回 Leader。
"""
from __future__ import annotations


class SpecialistError(Exception):
    """Specialist 异常基类。带 details dict，__str__ 自动展开。

    details 展开格式：``message (k='v', k2='v2')``。
    """

    def __init__(self, message: str = "", *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        """有 details 时展开为 ``message (k='v', ...)``，否则只返回 message。"""
        if not self.details:
            return self.message
        parts = [f"{k}={v!r}" for k, v in self.details.items()]
        return f"{self.message} ({', '.join(parts)})"


class SpecialistTimeoutError(SpecialistError):
    """specialist 超时（timeout_seconds 触发 kill）。"""

    def __init__(
        self,
        message: str = "specialist timeout",
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        details = {"timeout_seconds": timeout_seconds} if timeout_seconds is not None else {}
        super().__init__(message, details=details)


class SpecialistCrashError(SpecialistError):
    """specialist 子进程异常退出（含 exit_code）。"""

    def __init__(
        self,
        message: str = "specialist crash",
        *,
        exit_code: int | None = None,
    ) -> None:
        details = {"exit_code": exit_code} if exit_code is not None else {}
        super().__init__(message, details=details)


class SpecialistStartupError(SpecialistError):
    """specialist 启动失败（握手失败 / 子进程 spawn 失败）。"""


class SpecialistCredentialError(SpecialistError):
    """specialist 凭证缺失或失效。该 specialist 不进 LLM 主态，标 disabled。"""


class SpecialistNotFoundError(SpecialistError):
    """specialist 未注册。Registry.get 查不到时抛此，details 固定带 name。"""

    def __init__(self, name: str) -> None:
        super().__init__(
            f"specialist not registered: {name}",
            details={"name": name},
        )


class SubagentError(Exception):
    """Subagent 异常基类（独立层次，不继承 SpecialistError）。

    subagent 是 Poirot 内部 self-copy，不属 specialist 黑盒，故独立成层。
    """

    def __init__(self, message: str = "", *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        """有 details 时展开为 ``message (k='v', ...)``，否则只返回 message。"""
        if not self.details:
            return self.message
        parts = [f"{k}={v!r}" for k, v in self.details.items()]
        return f"{self.message} ({', '.join(parts)})"


class SubagentTimeoutError(SubagentError):
    """subagent 超时（timeout_seconds 触发中断）。"""


class SubagentMaxStepsError(SubagentError):
    """subagent 超过 max_steps 限制（leaf role 递归控制）。"""

    def __init__(
        self,
        message: str = "subagent exceeded max_steps",
        *,
        max_steps: int | None = None,
    ) -> None:
        details = {"max_steps": max_steps} if max_steps is not None else {}
        super().__init__(message, details=details)