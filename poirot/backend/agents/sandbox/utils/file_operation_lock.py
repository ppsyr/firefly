"""文件操作锁 — 进程内 WeakValueDictionary，按 (sandbox_id, virtual_path) 串行化文件操作。

【整体职责】
为同一沙箱内同一虚拟路径的文件操作提供进程内互斥锁，避免并发"读—改—写"
互相覆盖（如 str_replace 的 read → replace → write）。锁按 (sandbox_id,
virtual_path) 维度隔离：不同沙箱、不同文件互不阻塞。

【内容摘要】
- _LockKey                    : 锁 key 类型别名，形如 (sandbox_id, virtual_path)。
- _FILE_OPERATION_LOCKS       : WeakValueDictionary，key → Lock；锁无引用时自动回收。
- _FILE_OPERATION_LOCKS_GUARD : 保护字典本身的元锁，保证 get-or-create 原子。
- get_file_operation_lock_key : 构造锁 key（用虚拟路径，非物理路径）。
- get_file_operation_lock     : 取锁；不存在则创建。工具层调用入口。

【职责边界】
- 只负责：进程内按 (sandbox_id, virtual_path) 分配互斥锁。
- 不负责：跨进程 / 跨容器的互斥（见【已知限制】）、文件操作的原子性保证
  （本模块只提供锁，不保证调用方正确使用）。

【已知限制（S11 文档声明）】
- 锁仅进程内，不跨进程。Docker 模式下 read_file + write_file 是两次独立 HTTP 调用：
    别进程修改文件 → read 拿旧内容 → write 覆盖别进程的修改 → 静默丢数据（TOCTOU）。
- 当前 Poirot 单进程运行，此限制可接受。
- 多 worker 部署需引入容器内 flock 或 SDK 原子接口。
- 长期方案见 design_docs/todo_docs/02-sandbox-security-vulnerabilities-fix.md §11 B6。

【INVARIANT】
- key 用虚拟路径（工具层拿到的是虚拟路径），不用物理路径。
- WeakValueDictionary：锁无外部引用时自动 GC，防长跑进程内存泄漏。
- get-or-create 在元锁 _FILE_OPERATION_LOCKS_GUARD 保护下完成，保证原子。
- 元锁只保护字典读写；不保护文件操作本身（文件操作用返回的 lock）。
"""
from __future__ import annotations

import threading
import weakref

_LockKey = tuple[str, str]  # (sandbox_id, virtual_path)
_FILE_OPERATION_LOCKS: weakref.WeakValueDictionary[_LockKey, threading.Lock] = (
    weakref.WeakValueDictionary()
)
_FILE_OPERATION_LOCKS_GUARD = threading.Lock()


def get_file_operation_lock_key(sandbox_id: str, virtual_path: str) -> _LockKey:
    """构造锁 key：用虚拟路径（工具层拿到的是虚拟路径）。"""
    return (sandbox_id, virtual_path)


def get_file_operation_lock(sandbox_id: str, virtual_path: str) -> threading.Lock:
    """获取 (sandbox_id, virtual_path) 对应的锁；不存在则创建。

    - WeakValueDictionary：锁无引用时自动 GC，防长跑进程内存泄漏。
    - key 用虚拟路径。
    """
    lock_key = get_file_operation_lock_key(sandbox_id, virtual_path)
    with _FILE_OPERATION_LOCKS_GUARD:
        lock = _FILE_OPERATION_LOCKS.get(lock_key)
        if lock is None:
            lock = threading.Lock()
            _FILE_OPERATION_LOCKS[lock_key] = lock
        return lock