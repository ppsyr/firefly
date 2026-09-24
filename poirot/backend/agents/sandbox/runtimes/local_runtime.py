"""LocalRuntime — 本地运行时（subprocess 裸执行 + Python 标准库文件操作）。

【整体职责】
实现 sandbox_runtime 契约的"本地版本"：用 subprocess 执行命令，用 pathlib / os
做文件读写、列目录、glob、grep 等操作。只负责"裸执行"——不知道路径翻译、
不做安全检查；这些由 translator / guard 在 Sandbox 门面层完成。
所有异常统一包装成 SandboxError 子类（Grill #5），不向调用方泄漏内置异常。

【内容摘要】
- 模块常量：
    _EXEC_TIMEOUT_SECONDS      : 命令执行超时（600 秒）。
    _MAX_REGEX_PATTERN_LENGTH  : grep 正则最大长度（200 字符，S10 ReDoS 防护）。
    _NESTED_QUANTIFIER_RE      : 嵌套量词检测正则（如 (a+)+，ReDoS 经典模式）。
- 模块函数：
    _validate_regex_pattern    : ReDoS 防护；拒绝超长 pattern + 嵌套量词。
    _is_path_ignored           : 路径任一段命中 IGNORE_PATTERNS 即忽略。
- LocalRuntime                 : 本地运行时类。
    ├─ __init__      : 持有 allow_host_bash 开关。
    ├─ exec_command  : subprocess 执行命令（shell=True + 超时 + 异常包装）。
    ├─ read_file     : 读文本文件。
    ├─ write_file    : 写 / 追加文本文件（自动建父目录）。
    ├─ list_dir      : BFS 剪枝列目录（S9，防大目录 DoS）。
    ├─ _scan_bfs     : BFS 递归扫描（list_dir 内部用）。
    ├─ glob          : 按 pattern 递归匹配（带 max_results 截断）。
    ├─ grep          : 正则搜索文件内容（带 ReDoS 防护 + 忽略过滤 + 大小限制）。
    ├─ download_file : 读二进制文件（供 artifact 下载）。
    ├─ update_file   : 写二进制文件（自动建父目录）。
    └─ close         : no-op（subprocess 无持久连接）。

【职责边界】
- 只负责：命令的裸执行 + 本地文件系统操作。
- 不负责：路径翻译（translator）、安全检查（guard）、沙箱创建 / 销毁
  （provider）、写入大小限制（Grill #7：Stage 3 工具层加，不在本层）。
- 持有状态：仅 allow_host_bash 开关，无持久连接 / 无缓存。

【INVARIANT】
- exec_command 用 subprocess.run(shell=True)，捕获超时 → SandboxCommandError，
  非零退出 → SandboxCommandError。
- allow_host_bash=False 时 exec_command 直接 raise SandboxRuntimeError（S2 安全加固）。
- 文件操作异常包装：FileNotFoundError → SandboxFileNotFoundError；
  PermissionError → SandboxPermissionError；均携带 path + operation。
- grep 用 IGNORE_PATTERNS 过滤 + DEFAULT_MAX_FILE_SIZE_BYTES 跳过大文件。
- grep 非 literal 模式必须先过 _validate_regex_pattern（S10 ReDoS 防护）。
- list_dir 用 BFS 剪枝（S9）：超 max_depth 不递归，超 max_entries 截断。
- close 为 no-op（subprocess 无持久连接）。
- 无 write_file 80KB 限制（Grill #7）：裸执行不关心 LLM chunk 大小，
  该限制在 Stage 3 工具层加。
- 所有对外抛出的异常均为 SandboxError 子类（Grill #5）。
"""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from pathlib import Path

from poirot.backend.agents.sandbox.exceptions import (
    SandboxCommandError,
    SandboxFileNotFoundError,
    SandboxPermissionError,
    SandboxRuntimeError,
)
from poirot.backend.agents.sandbox.types import GrepMatch
from poirot.backend.agents.sandbox.utils.search import (
    DEFAULT_LINE_SUMMARY_LENGTH,
    DEFAULT_MAX_FILE_SIZE_BYTES,
    IGNORE_PATTERNS,
)

_EXEC_TIMEOUT_SECONDS = 600

# S10: ReDoS 防护
_MAX_REGEX_PATTERN_LENGTH = 200
# 检测嵌套量词：(group with quantifier) followed by quantifier — ReDoS 经典模式
_NESTED_QUANTIFIER_RE = re.compile(r"\([^)]*[+*?][^)]*\)[+*?]")


def _validate_regex_pattern(pattern: str) -> None:
    """ReDoS 防护：拒绝超长 pattern + 嵌套量词（如 (a+)+）。

    Python re 无 timeout 机制，用户控制 pattern + 无防护 = 灾难回溯 DoS。
    """
    if len(pattern) > _MAX_REGEX_PATTERN_LENGTH:
        raise ValueError(
            f"regex pattern too long ({len(pattern)} > {_MAX_REGEX_PATTERN_LENGTH} chars)"
        )
    if _NESTED_QUANTIFIER_RE.search(pattern):
        raise ValueError(
            f"potential ReDoS pattern (nested quantifier): {pattern[:50]}"
        )


def _is_path_ignored(file_path: Path, root: Path) -> bool:
    """路径的任一段命中 IGNORE_PATTERNS 即忽略（含目录名如 .git）。

    file_path 不在 root 下时返回 False。
    """
    try:
        rel = file_path.relative_to(root)
    except ValueError:
        return False
    for part in rel.parts:
        for pat in IGNORE_PATTERNS:
            if fnmatch.fnmatch(part, pat):
                return True
    return False


class LocalRuntime:
    """本地运行时：subprocess 裸执行 + Python 标准库文件操作。

    只负责裸执行——不知路径翻译、不做安全检查。
    全抛 SandboxError 子类（Grill #5），包装内置异常。

    核心约束见模块级 INVARIANT。
    """

    def __init__(self, allow_host_bash: bool = True) -> None:
        self._allow_host_bash = allow_host_bash

    def exec_command(self, command: str) -> str:
        """执行 shell 命令，返回 stdout。

        - allow_host_bash=False → raise SandboxRuntimeError（S2 安全加固）。
        - 超时 → SandboxCommandError；非零退出 → SandboxCommandError。
        """
        if not self._allow_host_bash:
            raise SandboxRuntimeError(
                "host bash is disabled (POIROT_SANDBOX_ALLOW_HOST_BASH=false)"
            )
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=_EXEC_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise SandboxCommandError(
                "command timed out", command=command, exit_code=None
            ) from exc
        if result.returncode != 0:
            raise SandboxCommandError(
                f"command failed with exit code {result.returncode}",
                command=command,
                exit_code=result.returncode,
            )
        return result.stdout

    def read_file(self, path: str) -> str:
        """读文本文件；FileNotFoundError / PermissionError 包装为 SandboxError 子类。"""
        try:
            return Path(path).read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise SandboxFileNotFoundError(
                f"file not found: {path}", path=path, operation="read"
            ) from exc
        except PermissionError as exc:
            raise SandboxPermissionError(
                f"permission denied: {path}", path=path, operation="read"
            ) from exc

    def write_file(self, path: str, content: str, append: bool = False) -> None:
        """写 / 追加文本文件；自动创建父目录；PermissionError 包装。"""
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            if append:
                with p.open("a", encoding="utf-8") as f:
                    f.write(content)
            else:
                p.write_text(content, encoding="utf-8")
        except PermissionError as exc:
            raise SandboxPermissionError(
                f"permission denied: {path}", path=path, operation="write"
            ) from exc

    def list_dir(self, path: str, max_depth: int = 2, max_entries: int = 1000) -> list[str]:
        """BFS 剪枝遍历——os.scandir 按层下钻，超 max_depth 不递归，超 max_entries 截断。

        S9: 替代 rglob("*") 先全遍历再过滤的反模式——node_modules 10 万文件不再 DoS。
        """
        try:
            root = Path(path)
            if not root.exists():
                raise SandboxFileNotFoundError(
                    f"dir not found: {path}", path=path, operation="list_dir"
                )
            entries: list[str] = []
            self._scan_bfs(root, root, 1, max_depth, max_entries, entries)
            return sorted(entries)
        except PermissionError as exc:
            raise SandboxPermissionError(
                f"permission denied: {path}", path=path, operation="list_dir"
            ) from exc

    @staticmethod
    def _scan_bfs(
        root: Path, current: Path, depth: int, max_depth: int,
        max_entries: int, entries: list[str],
    ) -> None:
        """BFS 递归扫描——depth = 当前层条目的路径段数（root 直接子项 depth=1）。"""
        if depth > max_depth or len(entries) >= max_entries:
            return
        try:
            with os.scandir(current) as it:
                for entry in sorted(it, key=lambda e: e.name):
                    if len(entries) >= max_entries:
                        return
                    rel = str(Path(entry.path).relative_to(root))
                    entries.append(rel)
                    if entry.is_dir() and depth < max_depth:
                        LocalRuntime._scan_bfs(
                            root, Path(entry.path), depth + 1, max_depth, max_entries, entries,
                        )
        except (PermissionError, OSError):
            pass  # 跳过不可读目录

    def glob(
        self,
        path: str,
        pattern: str,
        *,
        include_dirs: bool = False,
        max_results: int = 200,
    ) -> tuple[list[str], bool]:
        """按 pattern 递归匹配；返回 (matches, truncated)。

        truncated=True 表示达到 max_results 提前截断。
        PermissionError 包装为 SandboxPermissionError。
        """
        try:
            root = Path(path)
            matches: list[str] = []
            for item in root.rglob(pattern):
                if not include_dirs and item.is_dir():
                    continue
                matches.append(str(item.relative_to(root)))
                if len(matches) >= max_results:
                    return matches, True
            return matches, False
        except PermissionError as exc:
            raise SandboxPermissionError(
                f"permission denied: {path}", path=path, operation="glob"
            ) from exc

    def grep(
        self,
        path: str,
        pattern: str,
        *,
        glob: str | None = None,
        literal: bool = False,
        case_sensitive: bool = False,
        max_results: int = 100,
    ) -> tuple[list[GrepMatch], bool]:
        """正则搜索文件内容；返回 (matches, truncated)。

        - literal=True 时按字面量匹配（re.escape），不过 ReDoS 校验。
        - 非 literal 模式必须先过 _validate_regex_pattern（S10 ReDoS 防护）。
        - 用 IGNORE_PATTERNS 过滤路径 + DEFAULT_MAX_FILE_SIZE_BYTES 跳过大文件。
        - 每行命中结果截断到 DEFAULT_LINE_SUMMARY_LENGTH。
        """
        try:
            root = Path(path)
            flags = 0 if case_sensitive else re.IGNORECASE
            if literal:
                regex = re.compile(re.escape(pattern), flags)
            else:
                # S10: ReDoS 防护——非 literal 模式校验 pattern
                _validate_regex_pattern(pattern)
                regex = re.compile(pattern, flags)
            matches: list[GrepMatch] = []
            for file_path in root.rglob(glob or "*"):
                if file_path.is_dir():
                    continue
                if _is_path_ignored(file_path, root):
                    continue
                try:
                    stat = file_path.stat()
                except OSError:
                    continue
                if stat.st_size > DEFAULT_MAX_FILE_SIZE_BYTES:
                    continue
                try:
                    content = file_path.read_text(encoding="utf-8", errors="replace")
                except (PermissionError, OSError):
                    continue
                for line_num, line in enumerate(content.splitlines(), start=1):
                    if regex.search(line):
                        truncated = line[:DEFAULT_LINE_SUMMARY_LENGTH]
                        matches.append(
                            GrepMatch(str(file_path), line_num, truncated)
                        )
                        if len(matches) >= max_results:
                            return matches, True
            return matches, False
        except PermissionError as exc:
            raise SandboxPermissionError(
                f"permission denied: {path}", path=path, operation="grep"
            ) from exc

    def download_file(self, path: str) -> bytes:
        """读二进制文件（供 artifact 下载）；FileNotFoundError 包装。"""
        try:
            return Path(path).read_bytes()
        except FileNotFoundError as exc:
            raise SandboxFileNotFoundError(
                f"file not found: {path}", path=path, operation="download"
            ) from exc

    def update_file(self, path: str, content: bytes) -> None:
        """写二进制文件（自动建父目录）；PermissionError 包装。"""
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content)
        except PermissionError as exc:
            raise SandboxPermissionError(
                f"permission denied: {path}", path=path, operation="update"
            ) from exc

    def close(self) -> None:
        """no-op：subprocess 无持久连接，无需清理。"""
        pass