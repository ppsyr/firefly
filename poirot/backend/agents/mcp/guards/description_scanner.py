"""描述扫描 — 注册前检测工具描述中的 prompt injection。

【整体职责】
实现 SecurityGuard 契约中的 scan_description：在 MCP 工具注册前，扫描其描述文本，
若命中 prompt injection 可疑模式则拒绝注册（返 True）。其余两个接口为 no-op
（不参与环境过滤与错误脱敏）。

【内容摘要】
- _SUSPICIOUS_PATTERNS : 可疑模式正则列表（5 种）。
- DescriptionScanner.check_env       : no-op（返原 env）。
- DescriptionScanner.sanitize_error  : no-op（返原文本）。
- DescriptionScanner.scan_description: 命中可疑模式返 True（拒绝注册），否则 False。

【职责边界】
- 只负责：扫描工具描述、判定是否可疑。
- 不负责：环境变量过滤（EnvFilter）、错误脱敏（CredentialSanitizer）、
  扫描的调用时机（loader 的 _register_tools 决定何时调）。

【INVARIANT】
- 检测模式：ignore previous instructions / system prompt / you are now /
  forget everything / reveal your instructions。
- 检测到可疑模式返 True（拒绝注册），False 放行。
- check_env / sanitize_error no-op（返原值）。

【设计说明】
- 实现 SecurityGuard Protocol（鸭子类型，无需显式继承）。
- 只关注"描述扫描"这一职责——防 prompt injection 通过工具描述注入。
- 大小写不敏感（所有模式带 (?i)）。
- 可组合：与 EnvFilter / CredentialSanitizer 一起按顺序应用。
"""
from __future__ import annotations

import re

_SUSPICIOUS_PATTERNS = [
    re.compile(r"(?i)ignore\s+(previous|above|prior)\s+instructions"),
    re.compile(r"(?i)system\s+prompt"),
    re.compile(r"(?i)you\s+are\s+now"),
    re.compile(r"(?i)forget\s+(everything|prior|all)"),
    re.compile(r"(?i)reveal\s+(your|the)\s+(instructions?|prompt)"),
]


class DescriptionScanner:
    """描述扫描 guard。scan_description 检测，其余接口 no-op。

    实现 SecurityGuard 契约，仅负责"工具描述扫描"这一职责。
    """

    def check_env(self, env: dict[str, str]) -> dict[str, str]:
        """no-op：描述扫描不参与环境过滤。

        Args:
            env: 原始环境变量。

        Returns:
            dict[str, str]: 原样返回。
        """
        return env

    def sanitize_error(self, error_text: str) -> str:
        """no-op：描述扫描不参与错误脱敏。

        Args:
            error_text: 原始错误信息。

        Returns:
            str: 原样返回。
        """
        return error_text

    def scan_description(self, tool_name: str, description: str) -> bool:
        """检测到可疑模式返 True（拒绝注册），False 放行。

        Args:
            tool_name: 工具名。
            description: 工具描述。

        Returns:
            bool: True 表示拒绝注册，False 表示放行。
        """
        for pattern in _SUSPICIOUS_PATTERNS:
            if pattern.search(description):
                return True
        return False