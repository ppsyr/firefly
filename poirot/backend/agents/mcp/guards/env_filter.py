"""env 白名单过滤 — 防 stdio 子进程继承宿主 secrets。

【整体职责】
实现 SecurityGuard 契约中的 check_env：为 stdio 子进程构造一份"最小安全环境变量"，
只放行白名单键 + XDG_* 前缀，再合并 config 显式声明的 env，避免宿主 secrets
（AWS_KEY / GITHUB_TOKEN 等）随子进程泄漏。

【内容摘要】
- _SAFE_ENV_KEYS     : 安全 env 键白名单（POSIX 基础 + Windows 系统定位）。
- _SAFE_ENV_PREFIXES : 安全前缀（XDG_）。
- EnvFilter.check_env       : 白名单过滤 + 合并 config 声明 env。
- EnvFilter.sanitize_error  : no-op（返原文本）。
- EnvFilter.scan_description: no-op（返 False，放行）。

【职责边界】
- 只负责：构造 stdio 子进程的安全环境变量。
- 不负责：错误脱敏（CredentialSanitizer）、描述扫描（DescriptionScanner）、
  过滤的调用时机（loader 的 _connect_server 决定何时调）。

【INVARIANT】
- 安全 env 白名单：PATH / HOME / USER / LANG / LC_ALL / TERM / SHELL / TMPDIR
  + Windows 系统变量 + XDG_* 前缀。
- config 声明的 env 合并进白名单结果（显式声明优先，可覆盖）。
- 宿主其他变量（AWS_KEY / GITHUB_TOKEN 等）不泄露给子进程。
- sanitize_error / scan_description no-op（返原值 / 返 False）。

【设计说明】
- 实现 SecurityGuard Protocol（鸭子类型，无需显式继承）。
- 只关注"环境变量过滤"这一职责——防 stdio 子进程继承宿主凭证。
- 默认拒绝：不在白名单的键一律不传（fail-closed）。
- 显式声明优先：config 里 env 段的键值覆盖白名单结果（用户明确要传的就传）。
- 可组合：与 CredentialSanitizer / DescriptionScanner 一起按顺序应用。
"""
from __future__ import annotations

import os

_SAFE_ENV_KEYS = frozenset({
    # POSIX 基础
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "SHELL", "TMPDIR",
    # Windows 系统定位
    "ALLUSERSPROFILE", "APPDATA", "COMSPEC", "PROGRAMDATA",
    "PROGRAMFILES", "SYSTEMDRIVE", "SYSTEMROOT", "WINDIR",
    "USERPROFILE", "TEMP", "TMP",
})

_SAFE_ENV_PREFIXES = ("XDG_",)


class EnvFilter:
    """env 白名单 guard。check_env 过滤，其余接口 no-op。

    实现 SecurityGuard 契约，仅负责"环境变量白名单过滤"这一职责。
    """

    def check_env(self, env: dict[str, str]) -> dict[str, str]:
        """白名单过滤：宿主 env 仅安全键 + config 声明 env 合并。

        config 声明的 env 覆盖白名单结果（显式声明优先）。

        Args:
            env: config 显式声明的环境变量。

        Returns:
            dict[str, str]: 过滤后的安全环境变量（白名单 + config 声明）。
        """
        safe: dict[str, str] = {}
        for key, value in os.environ.items():
            if key in _SAFE_ENV_KEYS or any(key.startswith(p) for p in _SAFE_ENV_PREFIXES):
                safe[key] = value
        safe.update(env)
        return safe

    def sanitize_error(self, error_text: str) -> str:
        """no-op：env 过滤不参与错误脱敏。

        Args:
            error_text: 原始错误信息。

        Returns:
            str: 原样返回。
        """
        return error_text

    def scan_description(self, tool_name: str, description: str) -> bool:
        """no-op：env 过滤不参与描述扫描。

        Args:
            tool_name: 工具名。
            description: 工具描述。

        Returns:
            bool: 始终 False（放行）。
        """
        return False