"""Sandbox 搜索工具 — 忽略路径判定 + 行截断（供 grep / 搜索类工具复用）。

【整体职责】
为搜索类工具（grep 等）提供两个公共能力：
① 判断路径是否应被忽略（版本控制目录、依赖目录、缓存、编辑器临时文件等）；
② 把命中行截断到固定长度，避免超长行撑爆输出。
两者都是纯函数，无状态，供工具层直接调用。

【内容摘要】
- IGNORE_PATTERNS           : 忽略模式列表（目录名 / 文件名 glob 模式）。
- DEFAULT_MAX_FILE_SIZE_BYTES : 默认单文件大小上限（1_000_000 字节）。
- DEFAULT_LINE_SUMMARY_LENGTH: 默认单行截断长度（200 字符）。
- should_ignore_path        : 判断路径是否命中忽略模式（逐段匹配）。
- truncate_line             : 截断单行到指定长度，超出加 "..." 后缀。

【职责边界】
- 只负责：忽略判定（纯字符串匹配）、行截断（纯字符串处理）。
- 不负责：实际文件遍历 / 搜索（由调用方负责）、文件大小校验
  （DEFAULT_MAX_FILE_SIZE_BYTES 只是常量声明，判定由调用方使用）。
- 无状态：两个函数都是纯函数，不持有任何字段。

【INVARIANT】
- should_ignore_path 按路径"段"（segment）匹配，不按整条路径——
  只要任一段命中 IGNORE_PATTERNS 即忽略。
- 路径分隔符统一：先把 "\\" 替换成 "/" 再 split，跨平台一致。
- 跳过空段（split 后可能产生空字符串）。
- truncate_line 先 rstrip("\\n\\r") 去掉行尾换行，再判长度。
- 截断保留 max_chars 字符（含 "..."）：line[:max_chars - 3] + "..."。
- IGNORE_PATTERNS 是模块级常量，非配置——调用方如需自定义需另建列表。
"""
from __future__ import annotations

import fnmatch

IGNORE_PATTERNS: list[str] = [
    ".git", ".svn", ".hg", ".bzr",
    "node_modules", "__pycache__", ".venv", "venv", ".env", "env",
    ".tox", ".nox", ".eggs", "*.egg-info", "site-packages",
    "dist", "build", ".next", ".nuxt", ".output", ".turbo", "target", "out",
    ".idea", ".vscode", "*.swp", "*.swo", "*~",
    ".DS_Store", "Thumbs.db", "desktop.ini", "*.lnk",
    "*.log", "*.tmp", "*.temp", "*.bak", "*.cache",
    ".coverage", "coverage", ".nyc_output", "htmlcov",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
]

DEFAULT_MAX_FILE_SIZE_BYTES = 1_000_000
DEFAULT_LINE_SUMMARY_LENGTH = 200


def should_ignore_path(path: str) -> bool:
    """检查路径的任一段是否匹配 IGNORE_PATTERNS。

    先统一分隔符（\\ → /），再按 "/" 分段逐段匹配；
    任一段命中即返回 True。空段跳过。
    """
    for segment in path.replace("\\", "/").split("/"):
        if not segment:
            continue
        for pattern in IGNORE_PATTERNS:
            if fnmatch.fnmatch(segment, pattern):
                return True
    return False


def truncate_line(line: str, max_chars: int = DEFAULT_LINE_SUMMARY_LENGTH) -> str:
    """截断行到 max_chars，超出加 '...' 后缀。

    先去掉行尾换行（\\n / \\r）；长度不超则原样返回。
    """
    line = line.rstrip("\n\r")
    if len(line) <= max_chars:
        return line
    return line[: max_chars - 3] + "..."