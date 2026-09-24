"""熔断器 — per-tool 健康状态机。

【整体职责】
为每个工具维护一个健康状态机（closed / open / half_open），根据连续失败次数
和冷却时间决定是否放行调用，避免对持续失败的工具反复重试。

【内容摘要】
- _CIRCUIT_BREAKER_THRESHOLD / _CIRCUIT_BREAKER_COOLDOWN_SEC : 熔断阈值与冷却时长。
- CircuitBreaker.allow_call    : 是否放行本次调用（含状态转换）。
- CircuitBreaker.record_success: 记录成功（归零 + closed）。
- CircuitBreaker.record_failure: 记录失败（计数 + 触发 open / 重置 open）。

【职责边界】
- 只负责：维护单个工具的健康状态机、判定是否放行、记录成败。
- 不负责：工具的实际调用（handler）、审计与日志（McpAuditMiddleware）、
  熔断器实例的管理与查找（mcp/registry）。

【INVARIANT】
- closed: 正常态，allow_call 返 True。
- open: 拒绝调用，cooldown 后转 half_open 放探针。
- half_open: 放行 1 次探针，成功归 closed，失败重置 open。
- 线程安全：_lock 保护状态转换。
- 被动触发：不主动 ping，只在调用时更新状态。

【参数取值】
- 阈值：连续 3 次失败 → open。
- 冷却：open 后 60 秒 → 允许转 half_open 放探针。

【设计说明】
- 用 __slots__ 限制实例字段（省内存，每工具一个实例）。
- 三个状态组成确定性状态机：closed → open → half_open → closed / open。
- 非主动健康检查——由实际调用的成败驱动状态转换（被动触发）。
"""
from __future__ import annotations

import threading
import time

_CIRCUIT_BREAKER_THRESHOLD = 3
_CIRCUIT_BREAKER_COOLDOWN_SEC = 60.0


class CircuitBreaker:
    """每工具一个实例。被动触发，不主动 ping。

    Attributes:
        failure_count: 连续失败次数（成功归零）。
        state: 当前状态（closed / open / half_open）。
        opened_at: 进入 open 的时间戳。
        _lock: 保护状态转换的锁。
    """

    __slots__ = ("failure_count", "state", "opened_at", "_lock")

    def __init__(self) -> None:
        """初始化：closed、0 失败、未打开。"""
        self.failure_count: int = 0
        self.state: str = "closed"
        self.opened_at: float = 0.0
        self._lock = threading.Lock()

    def allow_call(self) -> bool:
        """判定是否放行本次调用（含状态转换）。

        - closed → True
        - open 且 cooldown 未过 → False
        - open 且 cooldown 过 → 转 half_open，返 True
        - half_open → 放一次探针，返 True

        Returns:
            bool: 是否放行。
        """
        with self._lock:
            if self.state == "closed":
                return True
            if self.state == "open":
                if time.time() - self.opened_at >= _CIRCUIT_BREAKER_COOLDOWN_SEC:
                    self.state = "half_open"
                    return True
                return False
            # half_open：只放一次探针
            return True

    def record_success(self) -> None:
        """成功归零，closed。"""
        with self._lock:
            self.failure_count = 0
            self.state = "closed"

    def record_failure(self) -> None:
        """失败计数++；closed 下连续 3 次→open；half_open 失败→重置 open。"""
        with self._lock:
            self.failure_count += 1
            if self.state == "half_open":
                self.state = "open"
                self.opened_at = time.time()
            elif self.state == "closed" and self.failure_count >= _CIRCUIT_BREAKER_THRESHOLD:
                self.state = "open"
                self.opened_at = time.time()