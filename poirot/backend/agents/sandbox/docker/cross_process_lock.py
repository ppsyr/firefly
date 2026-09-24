"""Cross-process file lock — fcntl (Unix) / msvcrt (Windows)。

【整体职责】
提供跨进程文件锁的三个基础函数：打开锁文件、获取排他锁、释放锁。
Unix 用 fcntl.flock，Windows 用 msvcrt.locking，运行时按平台自动分流。
供 DockerSandboxProvider 在 discover/create 时保护跨进程临界区使用。

【内容摘要】
- open_lock_file       : 打开锁文件（自动建父目录），返回文件对象。
- lock_file_exclusive  : 获取排他锁，阻塞直到获取。
- unlock_file          : 释放锁。

【职责边界】
- 只负责：三函数式的锁原语（open / lock / unlock）。
- 不负责：锁文件路径的选择（由调用方决定）、context manager 语义
  （刻意不提供，sync/async 统一用显式 open/lock/unlock/close）。
- 无状态：不持有任何字段；锁状态在文件对象上。

【INVARIANT】
- 仅导出 3 函数，无 context manager——sync/async 统一用显式调用序列：
    f = open_lock_file(path)
    try:
        lock_file_exclusive(f)
        ...
    finally:
        unlock_file(f)
        f.close()
- 同一文件对象贯穿 open → lock → unlock → close。
- Unix 用 fcntl.flock(LOCK_EX / LOCK_UN)；Windows 用 msvcrt.locking(LK_LOCK / LK_UNLCK)。
- Windows 路径需 seek(0) 后锁 1 字节（msvcrt 的锁定基于字节范围）。
- 平台判定在模块加载时完成：fcntl 导入失败即视为 Windows。
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
    import msvcrt


def open_lock_file(lock_path: Path):
    """打开锁文件（自动创建父目录），返回 append 模式的文件对象。"""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    return open(lock_path, "a", encoding="utf-8")


def lock_file_exclusive(lock_file) -> None:
    """获取排他锁，阻塞直到获取。

    Unix: fcntl.flock(LOCK_EX)
    Windows: msvcrt.locking(LK_LOCK)
    """
    if fcntl is not None:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        return
    lock_file.seek(0)
    msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)


def unlock_file(lock_file) -> None:
    """释放锁。

    Unix: fcntl.flock(LOCK_UN)
    Windows: msvcrt.locking(LK_UNLCK)
    """
    if fcntl is not None:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        return
    lock_file.seek(0)
    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)