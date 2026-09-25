"""MCP 安全层 — Protocol 基类 + 3 内置 guard。

【整体职责】
MCP 子系统的安全层出口：定义统一的 SecurityGuard 契约（3 接口），并提供 3 个
内置 guard 实现——分别负责 env 白名单过滤、凭证脱敏、描述扫描，由 loader 按顺序装载。

【内容摘要】
- SecurityGuard      : 安全检查 Protocol 基类（check_env / sanitize_error / scan_description）。
- EnvFilter          : env 白名单过滤（防宿主 secrets 泄露给 stdio 子进程）。
- CredentialSanitizer: 凭证脱敏（错误信息回 LLM 前清洗）。
- DescriptionScanner : 描述扫描（注册时检测 prompt injection）。

【职责边界】
- 只负责：定义安全契约、提供内置 guard 实现。
- 不负责：guard 的装载与调用（loader）、guard 的应用顺序编排（loader）、
  具体检查的调用时机（loader 的 _connect_server / _register_tools）。

【INVARIANT】
- SecurityGuard Protocol 定义 3 接口：check_env / sanitize_error / scan_description。
- 各 guard 只实现自己关心的接口，无关接口返原值（no-op）：
    · EnvFilter          → check_env（sanitize_error / scan_description no-op）
    · CredentialSanitizer→ sanitize_error（check_env / scan_description no-op）
    · DescriptionScanner → scan_description（check_env / sanitize_error no-op）
- loader 按顺序应用 guards（默认 [EnvFilter, DescriptionScanner]）。
- 可扩展：新增 guard 实现 Protocol 即可，loader 按配置装载。

【扩展点】
新增安全检查只需实现 SecurityGuard Protocol，注册到 loader 的 guards 列表即可，
无需改动 loader / McpManager。
"""
from poirot.backend.agents.mcp.guards.base import SecurityGuard
from poirot.backend.agents.mcp.guards.credential_sanitizer import CredentialSanitizer
from poirot.backend.agents.mcp.guards.description_scanner import DescriptionScanner
from poirot.backend.agents.mcp.guards.env_filter import EnvFilter

__all__ = [
    "SecurityGuard",
    "EnvFilter",
    "CredentialSanitizer",
    "DescriptionScanner",
]