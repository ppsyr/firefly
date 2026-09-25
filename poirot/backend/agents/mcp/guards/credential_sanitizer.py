"""凭证脱敏 — 错误信息回 LLM 前清洗 secrets。

【整体职责】
实现 SecurityGuard 契约中的 sanitize_error：把错误信息里的凭证（GitHub token /
OpenAI key / Bearer token / token= 形式）替换为 [REDACTED]，防止密钥泄漏到 LLM
或日志。其余两个接口为 no-op（不参与环境过滤与描述扫描）。

【内容摘要】
- _CREDENTIAL_PATTERNS : 凭证匹配正则列表（4 种模式）。
- _REDACTED            : 替换占位符。
- CredentialSanitizer.check_env       : no-op（返原 env）。
- CredentialSanitizer.sanitize_error  : 正则替换凭证为 [REDACTED]。
- CredentialSanitizer.scan_description: no-op（返 False，放行）。

【职责边界】
- 只负责：脱敏错误信息中的凭证。
- 不负责：环境变量过滤（EnvFilter）、工具描述扫描（DescriptionScanner）、
  脱敏的调用时机（McpAuditMiddleware / loader 决定何时调）。

【INVARIANT】
- 正则模式：ghp_* / sk-* / Bearer * / token=* → [REDACTED]。
- 仅作用于 sanitize_error（错误信息），不影响 tool result 正常内容。
- check_env / scan_description no-op（返原值 / 返 False）。

【设计说明】
- 实现 SecurityGuard Protocol（鸭子类型，无需显式继承）。
- 只关注"错误脱敏"这一职责，其他检查交由专门 guard（EnvFilter / DescriptionScanner）。
- 可组合：与 EnvFilter / DescriptionScanner 一起按顺序应用。
"""
from __future__ import annotations

import re

_CREDENTIAL_PATTERNS = [
    re.compile(r"ghp_[A-Za-z0-9]{36}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]+"),
    re.compile(r"token=[A-Za-z0-9]+"),
]

_REDACTED = "[REDACTED]"


class CredentialSanitizer:
    """凭证脱敏 guard。sanitize_error 清洗，其余接口 no-op。

    实现 SecurityGuard 契约，仅负责"错误信息脱敏"这一职责。
    """

    def check_env(self, env: dict[str, str]) -> dict[str, str]:
        """no-op：凭证脱敏不参与环境过滤。

        Args:
            env: 原始环境变量。

        Returns:
            dict[str, str]: 原样返回。
        """
        return env

    def sanitize_error(self, error_text: str) -> str:
        """正则替换凭证为 [REDACTED]。

        Args:
            error_text: 原始错误信息。

        Returns:
            str: 凭证已脱敏的错误信息。
        """
        result = error_text
        for pattern in _CREDENTIAL_PATTERNS:
            result = pattern.sub(_REDACTED, result)
        return result

    def scan_description(self, tool_name: str, description: str) -> bool:
        """no-op：凭证脱敏不参与描述扫描。

        Args:
            tool_name: 工具名。
            description: 工具描述。

        Returns:
            bool: 始终 False（放行）。
        """
        return False