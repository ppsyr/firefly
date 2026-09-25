"""安全层 Protocol 基类 — 可扩展的 MCP 安全检查接口。

【整体职责】
定义 MCP 安全检查层的统一契约：过滤环境变量、脱敏错误信息、扫描工具描述。
作为扩展点，新增 guard 实现本 Protocol 即可，loader 按配置装载；
多 guard 可组合，按顺序应用。

【内容摘要】
- SecurityGuard : 安全检查层 Protocol，含 check_env / sanitize_error / scan_description。

【职责边界】
- 只负责：定义安全检查的接口契约。
- 不负责：具体检查逻辑（各 guard 实现）、guard 的装载与调用（loader）、
  guard 的应用顺序编排（loader / McpManager）。

【INVARIANT】
- Protocol 契约：实现方无需显式继承，靠方法签名匹配通过 runtime_checkable 检查。
- 三个接口各 guard 按职责实现，无关接口返原值（no-op）。
- 可组合：多 guard 按顺序应用，各 guard 只处理自己关心的检查。
- 语义约定：
    · check_env      → 返过滤后的 env dict（stdio transport 用）；
    · sanitize_error → 返脱敏后的字符串（回 LLM 前用）；
    · scan_description → 返 True=拒绝注册，False=放行（注册前用）。

【扩展点】
新增 guard 实现 Protocol 即可，loader 按配置装载；不需要改 loader / McpManager。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SecurityGuard(Protocol):
    """MCP 安全检查层。可组合（多 guard 按顺序应用）。

    check_env: 过滤子进程环境变量（stdio transport 用）
    sanitize_error: 脱敏错误信息（回 LLM 前用）
    scan_description: 检测工具描述中的可疑模式（注册前用，返 True=拒绝）
    """

    def check_env(self, env: dict[str, str]) -> dict[str, str]:
        """过滤环境变量。返过滤后的 env dict。

        Args:
            env: 原始环境变量。

        Returns:
            dict[str, str]: 过滤后的环境变量。
        """
        ...

    def sanitize_error(self, error_text: str) -> str:
        """脱敏错误信息。返脱敏后的字符串。

        Args:
            error_text: 原始错误信息。

        Returns:
            str: 脱敏后的错误信息。
        """
        ...

    def scan_description(self, tool_name: str, description: str) -> bool:
        """检测工具描述是否可疑。返 True=拒绝注册，False=放行。

        Args:
            tool_name: 工具名。
            description: 工具描述。

        Returns:
            bool: True 表示拒绝注册，False 表示放行。
        """
        ...