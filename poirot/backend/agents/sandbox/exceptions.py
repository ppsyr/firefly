"""Sandbox 异常体系 — 统一的沙箱错误类型。

【整体职责】
定义 sandbox 层所有对外抛出的异常类型，形成一棵以 SandboxError 为根的继承树。
工具层只需 catch SandboxError 即可统一处理，由 ToolCallMiddleware 转 error ToolMessage。
runtime / guard / provider 等实现负责把底层异常（subprocess、FileNotFound 等）
包装成本体系的类型。

【内容摘要】
- SandboxError              : 错误基类，带 details dict，__str__ 自动展开。
- SandboxNotFoundError      : 沙箱找不到。
- SandboxRuntimeError       : runtime 不可用 / 配置错。
- SandboxCommandError       : 命令执行失败（command 自动截断 100 字符）。
- SandboxFileError          : 文件操作失败（path + operation）。
- SandboxPermissionError    : 权限拒绝（继承 SandboxFileError）。
- SandboxFileNotFoundError  : 文件不存在（继承 SandboxFileError）。

【职责边界】
- 只负责：定义异常类型、携带结构化 details、格式化错误信息。
- 不负责：异常抛出时机（runtime / guard / provider）、异常捕获与转换（middleware）。
- 不持有状态：异常对象只承载一次错误的信息，无生命周期。

【异常树结构】
SandboxError
├─ SandboxNotFoundError
├─ SandboxRuntimeError
├─ SandboxCommandError
└─ SandboxFileError
    ├─ SandboxPermissionError
    └─ SandboxFileNotFoundError

【INVARIANT】
- 所有异常继承自 SandboxError，工具层只需 catch SandboxError。
- details 是结构化数据（dict），供日志 / 上报 / 前端展示。
- SandboxCommandError 的 command 自动截断到 100 字符，避免超长命令撑爆日志。
- 文件相关异常（SandboxFileError 及子类）必须带 path + operation。
"""
from __future__ import annotations


class SandboxError(Exception):
    """Sandbox 错误基类。带 details dict，__str__ 自动展开。"""

    def __init__(self, message: str = "", *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        if not self.details:
            return self.message
        parts = [f"{k}={v!r}" for k, v in self.details.items()]
        return f"{self.message} ({', '.join(parts)})"


class SandboxNotFoundError(SandboxError):
    """沙箱找不到。"""

    def __init__(self, sandbox_id: str) -> None:
        super().__init__(
            f"sandbox not found: {sandbox_id}",
            details={"sandbox_id": sandbox_id},
        )


class SandboxRuntimeError(SandboxError):
    """runtime 不可用 / 配置错。"""


class SandboxCommandError(SandboxError):
    """命令执行失败。command 自动截断 100 字符。"""

    def __init__(
        self,
        message: str,
        *,
        command: str,
        exit_code: int | None = None,
    ) -> None:
        truncated = command[:100] + "..." if len(command) > 100 else command
        super().__init__(
            message,
            details={"command": truncated, "exit_code": exit_code},
        )


class SandboxFileError(SandboxError):
    """文件操作失败。"""

    def __init__(self, message: str, *, path: str, operation: str) -> None:
        super().__init__(
            message,
            details={"path": path, "operation": operation},
        )


class SandboxPermissionError(SandboxFileError):
    """权限拒绝。"""


class SandboxFileNotFoundError(SandboxFileError):
    """文件不存在。"""