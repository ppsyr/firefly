"""LocalSecurityGuard — 本地严格守卫（白名单 + 路径穿越拒绝 + 命令扫描）。

【整体职责】
实现 security_guard 契约的"本地严格"版本：对路径做白名单前缀校验、拒绝路径穿越，
对命令做危险模式黑名单 + 绝对路径白名单扫描。用于本地沙箱场景——
本地无容器隔离，安全完全依赖本 guard 的校验，故规则从严。
Docker / E2B 场景改用 PermissiveGuard（容器已隔离）。

【内容摘要】
- _SYSTEM_PATH_PREFIXES : 命令中允许出现的系统路径前缀白名单（/bin/ /usr/ /lib/）。
- _DANGEROUS_PATTERNS   : 危险命令黑名单（(regex, description)），S4 defense-in-depth。
- LocalSecurityGuard    : 本地严格守卫类。
    ├─ __init__                 : 持有 PathMapping 列表（白名单映射表）。
    ├─ _reject_path_traversal   : 拒绝含 ".." 段的路径（路径穿越）。
    ├─ _find_mapping            : 按最长前缀匹配查找 PathMapping。
    ├─ validate_path            : 路径校验（穿越 + 白名单 + 只读）。
    └─ validate_command         : 命令校验（危险模式 + shlex + 绝对路径白名单）。

【职责边界】
- 只负责：路径白名单校验、路径穿越拒绝、命令危险模式扫描、命令绝对路径白名单。
- 不负责：路径转换（translator）、命令执行（backend）、输出脱敏
  （本 guard 只做 validate，不做 mask_output）。
- 持有 PathMapping 列表：白名单规则的来源，由装配层注入。

【INVARIANT】
- validate_path 拒绝含 ".." 段的路径（路径穿越）。
- validate_path 按最长前缀匹配白名单：/mnt/poirot/user-data 可读写，/mnt/skills 只读；
  不在任何 mapping 内的路径一律拒绝。
- write=True 且命中的 mapping 为 read_only 时拒绝写入。
- validate_command 先扫危险模式黑名单（整条命令 pattern match），命中即拦。
- validate_command 的 shlex 解析失败 fail-closed：抛异常而非放行（防畸形引号绕过）。
- validate_command 扫描命令中的绝对路径 token：仅放行 _SYSTEM_PATH_PREFIXES
  或白名单 mapping 命中的路径。
- 系统路径放行 /bin/ /usr/ /lib/，不放行 /tmp/。
- 所有校验失败抛 SandboxPermissionError。
- 只做 validate，不做 mask_output。
"""
from __future__ import annotations

import re
import shlex
from pathlib import PurePosixPath

from poirot.backend.agents.sandbox.exceptions import SandboxPermissionError
from poirot.backend.agents.sandbox.types import PathMapping

_SYSTEM_PATH_PREFIXES = ("/bin/", "/usr/", "/lib/")

# 危险模式黑名单（S4 defense-in-depth）。
# 每项 (regex, description)。regex 用 word-boundary 降低误报。
# 非唯一防线——真正隔离靠 allow_host_bash gate（S2）。
_DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # 破坏性命令
    (re.compile(r"rm\s+-rf?\s+/(?!\w)"), "rm -rf /"),
    (re.compile(r"rm\s+-rf?\s+~(?!\w)"), "rm -rf ~"),
    (re.compile(r"rm\s+-rf?\s+\$HOME"), "rm -rf $HOME"),
    (re.compile(r"rm\s+-rf?\s+/\*"), "rm -rf /*"),
    (re.compile(r"mkfs\.\w+"), "mkfs filesystem format"),
    (re.compile(r"dd\s+.*\bof=/dev/"), "dd to device"),
    (re.compile(r":\s*\(\)\s*\{.*:.*\|.*:.*\}.*;"), "fork bomb"),
    # 远程代码执行
    (re.compile(r"curl\s+[^|]*\|\s*(bash|sh)\b"), "curl pipe to shell"),
    (re.compile(r"wget\s+[^|]*\|\s*(bash|sh)\b"), "wget pipe to shell"),
    # 危险内置命令
    (re.compile(r"\beval\b"), "eval builtin"),
    (re.compile(r"\bexec\b(?!\s*\.py)"), "exec builtin"),
    (re.compile(r"\bsource\b"), "source builtin"),
]


class LocalSecurityGuard:
    """本地严格守卫：白名单 + 路径穿越拒绝 + bash 命令扫描。

    Local 场景使用严格白名单；Docker / E2B 场景改用 PermissiveGuard
    （容器边界即安全边界，Guard 层不必重复校验）。

    核心约束见模块级 INVARIANT。
    """

    def __init__(self, path_mappings: list[PathMapping]) -> None:
        self._mappings = path_mappings

    def _reject_path_traversal(self, path: str) -> None:
        """含 ".." 段则判为路径穿越，抛 SandboxPermissionError。"""
        parts = PurePosixPath(path).parts
        if ".." in parts:
            raise SandboxPermissionError(
                f"path traversal detected: {path}", path=path, operation="validate"
            )

    def _find_mapping(self, path: str) -> PathMapping | None:
        """按最长前缀优先查找匹配的 PathMapping；无匹配返回 None。"""
        for mapping in sorted(
            self._mappings, key=lambda m: len(m.container_path), reverse=True
        ):
            container = mapping.container_path.rstrip("/")
            if path == container or path.startswith(container + "/"):
                return mapping
        return None

    def validate_path(self, path: str, *, write: bool = False) -> None:
        """路径校验：拒绝穿越 → 查白名单 → 只读映射禁止写入。"""
        self._reject_path_traversal(path)
        mapping = self._find_mapping(path)
        if mapping is None:
            raise SandboxPermissionError(
                f"path not in whitelist: {path}", path=path, operation="validate"
            )
        if write and mapping.read_only:
            raise SandboxPermissionError(
                f"write to read-only path: {path}", path=path, operation="write"
            )

    def validate_command(self, command: str) -> None:
        """命令校验：危险模式黑名单 → shlex 解析 → 绝对路径白名单。

        shlex 解析失败 fail-closed（抛 SandboxPermissionError），
        不放过畸形引号构造的绕过尝试。
        """
        # 危险模式黑名单（先于 shlex，直接对整条命令做 pattern match）
        for pattern, desc in _DANGEROUS_PATTERNS:
            if pattern.search(command):
                raise SandboxPermissionError(
                    f"dangerous command blocked: {desc}",
                    path=command[:100],
                    operation="validate_command",
                )

        # shlex 解析 → 绝对路径白名单检查
        try:
            tokens = shlex.split(command)
        except ValueError:
            # fail-closed：畸形引号导致解析失败 → 拒绝，不放行
            raise SandboxPermissionError(
                f"command has unparseable quoting: {command[:100]}",
                path=command[:100],
                operation="validate_command",
            )
        for token in tokens:
            if token.startswith("/"):
                if any(token.startswith(p) for p in _SYSTEM_PATH_PREFIXES):
                    continue
                mapping = self._find_mapping(token)
                if mapping is None:
                    raise SandboxPermissionError(
                        f"command references path not in whitelist: {token}",
                        path=token,
                        operation="validate_command",
                    )