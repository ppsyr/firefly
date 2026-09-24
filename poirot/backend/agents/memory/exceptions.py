"""Memory 模块异常层次。

【整体职责】
定义 memory 模块的错误信号。
基类 `MemoryError` 带 `details: dict` + `__str__` 自动展开（照抄 sandbox
`SandboxError` 模式）；5 个子类按操作语义携带上下文。

【组成】
基类：
- MemoryError（带 details + __str__ 自动展开）。

5 个子类（按操作语义携带上下文）：
- MemoryNotFoundError     : 记忆找不到       → trace_id。
- MemoryStoreError        : 存储失败         → operation + path。
- MemoryRetrieveError     : 检索失败         → query（超 100 字符截断）。
- MemoryConsolidateError  : 巩固失败         → trace_ids。
- MemoryConflictError     : 矛盾冲突         → old_id + new_id。

【职责边界】
- 本模块只定义异常类，不含任何逻辑（除 __init__ / __str__）。
- 不 import 项目内其他模块，是纯错误信号层。
- 被实现层（store / manager）抛出，被上层（middleware）捕获。

【INVARIANT】
- 异常是 fail-closed 信号：store / retrieve / consolidate 失败时抛
  MemoryError 子类，由 middleware 统一捕获转优雅降级（不静默吞）。
- 所有子类都继承 MemoryError，details 携带上下文。
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# 基类
# ---------------------------------------------------------------------------

class MemoryError(Exception):
    """Memory 错误基类。

    带 details dict，__str__ 自动展开。
    子类通过 super().__init__(message, details={...}) 传入上下文。

    Args:
        message: 错误描述（默认空串）。
        details: 上下文 dict（默认 None，内部转为空 dict）。
    """

    def __init__(self, message: str = "", *, details: dict | None = None) -> None:
        """初始化。

        Args:
            message: 错误描述。
            details: 上下文 dict（默认 None，内部转为空 dict）。

        Returns:
            None。

        Raises:
            不主动抛异常。

        组装规则：
            1. super().__init__(message)（把 message 传给 Exception）。
            2. self.message = message（便于 __str__ 使用）。
            3. self.details = details or {}（None 转空 dict）。
        """
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        """格式化错误字符串。

        Args:
            无。

        Returns:
            - details 为空：返回 message。
            - details 非空：返回 "message (k1=v1, k2=v2, ...)"。

        Raises:
            不主动抛异常。

        组装规则：
            1. details 为空 → 直接返回 self.message。
            2. 否则把 details 每项拼成 "k=v"（v 用 repr 保证字符串带引号）。
            3. 用 "message (k1=v1, k2=v2)" 格式返回。
        """
        if not self.details:
            return self.message
        parts = [f"{k}={v!r}" for k, v in self.details.items()]
        return f"{self.message} ({', '.join(parts)})"


# ---------------------------------------------------------------------------
# 子类（按操作语义携带上下文）
# ---------------------------------------------------------------------------

class MemoryNotFoundError(MemoryError):
    """记忆找不到。带 trace_id。

    Args:
        trace_id: 找不到的记忆 id。
    """

    def __init__(self, trace_id: str) -> None:
        """初始化。

        Args:
            trace_id: 找不到的记忆 id。

        Returns:
            None。

        Raises:
            不主动抛异常。

        组装规则：
            1. message = "memory trace not found: {trace_id}"。
            2. details = {"trace_id": trace_id}。
        """
        super().__init__(
            f"memory trace not found: {trace_id}",
            details={"trace_id": trace_id},
        )


class MemoryStoreError(MemoryError):
    """存储失败。带 operation + path。

    Args:
        message:   错误描述。
        operation: 操作名（add / update / remove 等）。
        path:      文件路径（默认空串）。
    """

    def __init__(self, message: str, *, operation: str, path: str = "") -> None:
        """初始化。

        Args:
            message:   错误描述。
            operation: 操作名（add / update / remove 等）。
            path:      文件路径（默认空串）。

        Returns:
            None。

        Raises:
            不主动抛异常。

        组装规则：
            1. message 原样传入。
            2. details = {"operation": operation, "path": path}。
        """
        super().__init__(message, details={"operation": operation, "path": path})


class MemoryRetrieveError(MemoryError):
    """检索失败。带 query（超 100 字符自动截断）。

    Args:
        message: 错误描述。
        query:   查询文本（超 100 字符自动截断）。
    """

    def __init__(self, message: str, *, query: str) -> None:
        """初始化。

        Args:
            message: 错误描述。
            query:   查询文本（超 100 字符自动截断）。

        Returns:
            None。

        Raises:
            不主动抛异常。

        组装规则：
            1. query 超 100 字符 → 截断为前 100 字符 + "..."。
            2. message 原样传入。
            3. details = {"query": truncated}。
        """
        truncated = query[:100] + "..." if len(query) > 100 else query
        super().__init__(message, details={"query": truncated})


class MemoryConsolidateError(MemoryError):
    """巩固失败。带 trace_ids。

    Args:
        message:   错误描述。
        trace_ids: 参与巩固的记忆 id 列表。
    """

    def __init__(self, message: str, *, trace_ids: list[str]) -> None:
        """初始化。

        Args:
            message:   错误描述。
            trace_ids: 参与巩固的记忆 id 列表。

        Returns:
            None。

        Raises:
            不主动抛异常。

        组装规则：
            1. message 原样传入。
            2. details = {"trace_ids": trace_ids}。
        """
        super().__init__(message, details={"trace_ids": trace_ids})


class MemoryConflictError(MemoryError):
    """矛盾冲突。带 old_id + new_id。

    Args:
        message: 错误描述。
        old_id:  旧记忆 id。
        new_id:  新记忆 id。
    """

    def __init__(self, message: str, *, old_id: str, new_id: str) -> None:
        """初始化。

        Args:
            message: 错误描述。
            old_id:  旧记忆 id。
            new_id:  新记忆 id。

        Returns:
            None。

        Raises:
            不主动抛异常。

        组装规则：
            1. message 原样传入。
            2. details = {"old_id": old_id, "new_id": new_id}。
        """
        super().__init__(message, details={"old_id": old_id, "new_id": new_id})