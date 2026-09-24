"""AuditGuard — 命令分级 + 审计日志（S8 defense-in-depth）。

【整体职责】
组合模式守卫：包装一个底层 guard（LocalSecurityGuard 或 PermissiveGuard），
在命令校验前先做三档分级（block / warn / pass），并把每次命令分级结果写入审计日志；
路径校验直接透传底层 guard。用于在"决策型 guard"之外叠加一层"观察 + 分级"能力。

【内容摘要】
- _BLOCK_PATTERNS : block 档正则表——破坏性命令，无论 Local/Docker 都拦截。
- _WARN_PATTERNS  : warn 档正则表——危险但非破坏，记日志放行（容器隔离兜底）。
- _classify       : 命令分级函数；返回 (level, matched_desc)，level ∈ {block, warn, pass}。
- AuditGuard      : 审计守卫类；组合底层 guard + 可选 journal。
    ├─ validate_path    : 路径校验，透传底层 guard。
    ├─ validate_command : 命令分级 + 审计 + 按档处理（block 拦截 / warn+pass 透传）。
    └─ _audit           : 写审计日志（logger + 可选 journal），失败不影响主流程。

【职责边界】
- 只负责：命令分级、审计记录、按档决定拦截或透传。
- 不负责：底层命令/路径规则（由 inner guard 负责）、真正执行（backend）、
  路径转换（translator）。
- 组合持有 inner guard：自身不实现底层规则，只在其外层叠加分级与审计。

【INVARIANT】
- 三档语义固定：
    block → 抛 SandboxPermissionError，直接拦截；
    warn  → 记审计日志后放行，继续透传底层 guard；
    pass  → 无特殊处理，透传底层 guard。
- block 优先于 warn：_classify 先扫 _BLOCK_PATTERNS，命中即返回，不再扫 warn。
- validate_path 不做分级，直接透传底层 guard。
- 审计写入失败（journal 抛异常）不影响主流程，静默吞掉。
- 命令截断：block 时 path 字段取 command[:100]；审计日志 command 取 command[:200]。
- 组合方式由模式决定：Local → AuditGuard(LocalSecurityGuard)；
  Docker → AuditGuard(PermissiveGuard)。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from poirot.backend.agents.sandbox.exceptions import SandboxPermissionError

_logger = logging.getLogger(__name__)

# block 档：破坏性命令，无论 Local/Docker 都拦截
_BLOCK_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"rm\s+-rf?\s+/(?!\w)"), "rm -rf /"),
    (re.compile(r"rm\s+-rf?\s+~(?!\w)"), "rm -rf ~"),
    (re.compile(r"rm\s+-rf?\s+\$HOME"), "rm -rf $HOME"),
    (re.compile(r"rm\s+-rf?\s+/\*"), "rm -rf /*"),
    (re.compile(r"mkfs\.\w+"), "mkfs"),
    (re.compile(r"dd\s+.*\bof=/dev/"), "dd to device"),
    (re.compile(r":\s*\(\)\s*\{.*:.*\|.*:.*\}.*;"), "fork bomb"),
]

# warn 档：危险但非破坏，记日志放行（容器隔离兜底）
_WARN_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"curl\s+[^|]*\|\s*(bash|sh)\b"), "curl pipe to shell"),
    (re.compile(r"wget\s+[^|]*\|\s*(bash|sh)\b"), "wget pipe to shell"),
    (re.compile(r"\bsudo\b"), "sudo"),
    (re.compile(r"\bchmod\s+777\b"), "chmod 777"),
    (re.compile(r"\beval\b"), "eval"),
    (re.compile(r"\bexec\b(?!\s*\.py)"), "exec builtin"),
]


def _classify(command: str) -> tuple[str, str | None]:
    """命令分级：返回 (level, matched_desc)。

    level = 'block' / 'warn' / 'pass'；
    matched_desc 为命中的规则描述，pass 档为 None。
    先扫 block，命中即返回；再扫 warn；都不命中则 pass。
    """
    for pattern, desc in _BLOCK_PATTERNS:
        if pattern.search(command):
            return ("block", desc)
    for pattern, desc in _WARN_PATTERNS:
        if pattern.search(command):
            return ("warn", desc)
    return ("pass", None)


class AuditGuard:
    """审计守卫——命令分级 + 审计日志，组合底层 guard。

    Args:
        inner: 底层 guard（LocalSecurityGuard / PermissiveGuard）
        journal: 可选 RunJournal，用于写 sandbox.command 审计事件
    """

    def __init__(self, inner: Any, journal: Any = None) -> None:
        self._inner = inner
        self._journal = journal

    def validate_path(self, path: str, *, write: bool = False) -> None:
        """路径校验透传底层 guard（本层不做分级）。"""
        self._inner.validate_path(path, write=write)

    def validate_command(self, command: str) -> None:
        """命令分级检查：block 拦截 / warn 记日志 / pass 透传。

        无论哪档都先记审计；block 直接抛异常，warn 与 pass 继续透传底层 guard。
        """
        level, desc = _classify(command)
        self._audit(command, level, desc)

        if level == "block":
            raise SandboxPermissionError(
                f"dangerous command blocked: {desc}",
                path=command[:100],
                operation="validate_command",
            )
        # warn + pass → 透传底层 guard 做进一步检查
        self._inner.validate_command(command)

    def _audit(self, command: str, level: str, desc: str | None) -> None:
        """写审计日志：logger 按档分级（block/warn → warning，pass → debug）；
        若注入 journal，则追加 sandbox.command 事件（失败静默吞掉）。
        """
        msg = f"sandbox.command level={level}"
        if desc:
            msg += f" desc={desc}"
        msg += f" cmd={command[:100]}"

        if level == "block":
            _logger.warning(msg)
        elif level == "warn":
            _logger.warning(msg)
        else:
            _logger.debug(msg)

        if self._journal is not None:
            try:
                self._journal.append("sandbox.command", {
                    "level": level,
                    "desc": desc or "",
                    "command": command[:200],
                })
            except Exception:
                pass  # 审计日志失败不影响主流程