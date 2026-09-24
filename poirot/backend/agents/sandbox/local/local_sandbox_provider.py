"""LocalSandboxProvider — 本地沙箱 provider（LRU 缓存 + 确定性 ID）。

【整体职责】
实现 SandboxProvider 契约的"本地版本"：按 thread_id 创建 / 复用本地沙箱实例，
用 LRU 缓存控制常驻数量，用确定性 ID 保证跨进程可推导。本地模式下
Sandbox 组合 LocalRuntime + LocalPathTranslator + AuditGuard(LocalSecurityGuard)。

【内容摘要】
- _DEFAULT_LRU_SIZE       : LRU 缓存默认容量（256）。
- _deterministic_sandbox_id: 由 user:thread 推导的确定性 sandbox_id（sha256[:8]）。
- LocalSandboxProvider    : 本地 provider 类。
    ├─ __init__   : 持有 path_mappings / LRU 容量 / sandbox 缓存 / 锁 / allow_host_bash。
    ├─ acquire    : 按 thread_id 取或建 sandbox；LRU 驱逐；返回 sandbox_id。
    ├─ get        : 纯内存查找，返回 Sandbox 或 None。
    ├─ release    : no-op（保留缓存，供下次复用）。
    ├─ reset      : 清空缓存（不 close）。
    └─ shutdown   : 清空缓存 + 锁外逐个 close。

【职责边界】
- 只负责：sandbox 的创建 / 缓存 / 查找 / 回收，以及本地组件组合。
- 不负责：runtime / translator / guard 的具体实现（各自独立模块）、
  sandbox 内的命令执行（Sandbox 门面）。
- 持有状态：path_mappings / LRU 容量 / OrderedDict 缓存 / 线程锁 / allow_host_bash。

【INVARIANT】
- acquire 必须传 thread_id；None 抛 ValueError（Grill #6，已去 legacy 单例）。
- sandbox_id 确定性：sha256(user:thread)[:8]，跨进程可推导。
- LRU 缓存默认 256 条，超出按 LRU 驱逐；驱逐前调 sandbox.close()。
- release 为 no-op：保留缓存，下次 acquire 复用。
- get 纯内存查找，不加锁以外的副作用，事件循环安全。
- Sandbox 组合：LocalRuntime + LocalPathTranslator + AuditGuard(LocalSecurityGuard)。
- _sandboxes 全部读写路径由 _lock 保护（严重1 修复）。
- LRU 驱逐：锁内 pop 元组，锁外调 sandbox.close()——避免持锁阻塞 I/O。
- 类属性 uses_thread_data_mounts=True / needs_upload_permission_adjustment=False。
"""
from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict

from poirot.backend.agents.sandbox.contracts import SandboxProvider
from poirot.backend.agents.sandbox.guards.audit_guard import AuditGuard
from poirot.backend.agents.sandbox.guards.local_security_guard import (
    LocalSecurityGuard,
)
from poirot.backend.agents.sandbox.runtimes.local_runtime import LocalRuntime
from poirot.backend.agents.sandbox.sandbox import Sandbox
from poirot.backend.agents.sandbox.translators.local_path_translator import (
    LocalPathTranslator,
)
from poirot.backend.agents.sandbox.types import PathMapping

_DEFAULT_LRU_SIZE = 256


def _deterministic_sandbox_id(user_id: str | None, thread_id: str | None) -> str:
    """确定性 sandbox_id = sha256(user:thread)[:8]；同一 (user, thread) 跨进程可推导。"""
    raw = f"{user_id or 'default'}:{thread_id or 'default'}"
    return hashlib.sha256(raw.encode()).hexdigest()[:8]


class LocalSandboxProvider(SandboxProvider):
    """本地 sandbox provider：LRU 缓存 + 确定性 ID。

    核心约束见模块级 INVARIANT。
    """

    uses_thread_data_mounts = True
    needs_upload_permission_adjustment = False

    def __init__(
        self,
        path_mappings: list[PathMapping] | None = None,
        lru_size: int = _DEFAULT_LRU_SIZE,
        sandbox_config=None,
    ) -> None:
        self._path_mappings = path_mappings or []
        self._lru_size = lru_size
        self._sandboxes: OrderedDict[str, Sandbox] = OrderedDict()
        self._lock = threading.Lock()
        self._allow_host_bash = True
        if sandbox_config is not None:
            self._allow_host_bash = getattr(sandbox_config, "allow_host_bash", True)

    def acquire(
        self, thread_id: str | None = None, *, user_id: str | None = None
    ) -> str:
        """按 thread_id 取或建 sandbox，返回 sandbox_id。

        - thread_id 必传，None 抛 ValueError。
        - 命中缓存：move_to_end 刷新 LRU 顺序后直接返回。
        - 未命中：组合 runtime / translator / guard 构造 Sandbox 并写入缓存；
          超出容量则驱逐最旧项（锁内 pop，锁外 close）。
        """
        if thread_id is None:
            raise ValueError(
                "thread_id is required (legacy singletons removed, Grill #6)"
            )
        sandbox_id = _deterministic_sandbox_id(user_id, thread_id)

        evicted: Sandbox | None = None
        with self._lock:
            if sandbox_id in self._sandboxes:
                self._sandboxes.move_to_end(sandbox_id)
                return sandbox_id

            runtime = LocalRuntime(allow_host_bash=self._allow_host_bash)
            translator = LocalPathTranslator(self._path_mappings)
            guard = AuditGuard(LocalSecurityGuard(self._path_mappings))
            sandbox = Sandbox(sandbox_id, runtime, translator, guard)
            self._sandboxes[sandbox_id] = sandbox

            if len(self._sandboxes) > self._lru_size:
                _evicted_id, evicted = self._sandboxes.popitem(last=False)

        # 锁外 close，避免持锁阻塞 I/O。被驱逐 sandbox 可能正被别线程用，
        # 但 LocalRuntime 的 close 是 no-op，不会炸。
        if evicted is not None:
            evicted.close()
        return sandbox_id

    def get(self, sandbox_id: str) -> Sandbox | None:
        """纯内存查找；未命中返回 None。"""
        with self._lock:
            return self._sandboxes.get(sandbox_id)

    def release(self, sandbox_id: str) -> None:
        """no-op：保留缓存，供下次 acquire 复用。"""
        pass

    def reset(self) -> None:
        """清空缓存（不 close）。"""
        with self._lock:
            self._sandboxes.clear()

    def shutdown(self) -> None:
        """清空缓存 + 锁外逐个 close。"""
        with self._lock:
            sandboxes = list(self._sandboxes.values())
            self._sandboxes.clear()
        # 锁外逐个 close，避免持锁时阻塞
        for sandbox in sandboxes:
            sandbox.close()