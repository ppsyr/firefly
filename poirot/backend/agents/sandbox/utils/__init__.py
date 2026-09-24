"""Sandbox 无状态工具层 — 供工具层与各实现复用的公共工具集。

【整体职责】
汇总 sandbox 层的公共工具：文件操作锁、sandbox_id 校验、搜索辅助。
这些工具不依赖具体隔离实现（local / docker），行为跨实现一致。

【内容摘要】
- file_operation_lock : 按 (sandbox_id, virtual_path) 分配进程内互斥锁。
- sandbox_id          : sandbox_id 格式校验（8 位小写十六进制）。
- search              : 忽略路径判定 + 行截断（供搜索类工具复用）。

【职责边界】
- 只负责：提供无状态（或进程内单例）的通用工具函数 / 常量。
- 不负责：具体隔离实现（local / docker）、路径翻译、安全校验。

【INVARIANT】
- 工具不依赖具体隔离实现，local / docker 下行为一致。
- 本文件只做 re-export，不含任何逻辑。
"""
from poirot.backend.agents.sandbox.utils.file_operation_lock import (
    get_file_operation_lock,
    get_file_operation_lock_key,
)
from poirot.backend.agents.sandbox.utils.sandbox_id import validate_sandbox_id
from poirot.backend.agents.sandbox.utils.search import (
    should_ignore_path,
    truncate_line,
)

__all__ = [
    "get_file_operation_lock",
    "get_file_operation_lock_key",
    "validate_sandbox_id",
    "should_ignore_path",
    "truncate_line",
]