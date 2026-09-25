"""MCP 管理模块层 — 配置化注册、熔断器、fallback、安全层、审计。

【整体职责】
作为 MCP 子系统的对外门面与公共 API 聚合点：把 config（YAML 配置）、registry
（工具注册表）、loader（连接生命周期）、audit（审计中间件）聚合成 McpManager，
并提供 build_mcp_manager 工厂（读开关 + 加载配置）。

【内容摘要】
- McpManager.__init__          : 聚合 config / registry / loader / audit。
- McpManager.load_startup      : eager 并行连接所有 enabled server。
- McpManager.add_server        : 运行时加载单个 server（串行加锁）。
- McpManager._persist_server   : 持久化 server 配置到 YAML。
- McpManager.list_servers      : 返回 server 列表 + 状态（供 TUI）。
- McpManager.get_tools         : 按 group 返回工具列表（供 agent 注入）。
- McpManager.get_audit_middleware : 供 middleware 链注入。
- McpManager.registry / loader : 暴露内部组件（供外化层 / reload）。
- McpManager.shutdown          : 清理所有连接。
- build_mcp_manager            : 从 .env 开关 + YAML 构建 McpManager。

【职责边界】
- 只负责：聚合各组件、提供对外 API、编排加载/清理。
- 不负责：各组件内部实现（config / registry / loader / audit 各模块）、
  工具的运行时调用与熔断（McpAuditMiddleware + CircuitBreaker）。

【INVARIANT】
- bootstrap 构造一次，随 AppRuntime 生命周期。
- load_startup() eager 并行连接，失败不阻塞。
- add_server() 运行时单 server 加载，串行加锁。
- get_tools(groups) 供 agent 注入。
- get_audit_middleware() 供 middleware 链注入。
- shutdown() 清理连接。
- switch_expert_mode 不重建（只调 get_tools 切 group）。
- build_mcp_manager：enabled=false 或无配置 → None（opt-in）。
- add_server 失败不改 registry（原子性）。

【公共 API】
config / registry / health / loader / audit / 门面 McpManager。
"""
import asyncio
import os

from poirot.backend.agents.mcp.audit import McpAuditMiddleware
from poirot.backend.agents.mcp.config import (
    McpConfig,
    McpServerConfig,
    load_mcp_config,
    save_mcp_config,
)
from poirot.backend.agents.mcp.health import CircuitBreaker
from poirot.backend.agents.mcp.loader import McpLoader
from poirot.backend.agents.mcp.registry import ToolEntry, ToolRegistry

__all__ = [
    "McpConfig",
    "McpServerConfig",
    "load_mcp_config",
    "save_mcp_config",
    "CircuitBreaker",
    "ToolEntry",
    "ToolRegistry",
    "McpLoader",
    "McpAuditMiddleware",
    "McpManager",
    "build_mcp_manager",
]


class McpManager:
    """MCP 管理门面。聚合 config + registry + loader + audit。

    INVARIANT:
    - bootstrap 构造一次，随 AppRuntime 生命周期
    - load_startup() eager 并行连接，失败不阻塞
    - add_server() 运行时单 server 加载，串行加锁
    - get_tools(groups) 供 agent 注入
    - get_audit_middleware() 供 middleware 链注入
    - shutdown() 清理连接
    - switch_expert_mode 不重建（只调 get_tools 切 group）

    Attributes:
        _config: MCP 配置。
        _registry: 工具注册表。
        _loader: 连接生命周期管理器。
        _audit: 审计中间件。
        _add_lock: add_server 串行锁。
    """

    def __init__(self, config: McpConfig) -> None:
        """初始化。

        Args:
            config: MCP 配置。
        """
        self._config = config
        self._registry = ToolRegistry(config)
        self._loader = McpLoader(config, self._registry)
        self._audit = McpAuditMiddleware(self._registry, self._loader.sanitizer)
        self._add_lock = asyncio.Lock()

    async def load_startup(self) -> None:
        """eager 并行连接所有 enabled server，失败不阻塞。"""
        await self._loader.load_startup()

    async def add_server(self, server_config: McpServerConfig) -> bool:
        """运行时加载单个 MCP server。串行加锁。

        成功 → 注册到 registry + 持久化 + 返 True。
        失败 → logger.error，不改 registry，返 False。

        Args:
            server_config: 待加载的 server 配置。

        Returns:
            bool: 是否加载成功。
        """
        async with self._add_lock:
            try:
                tools = await self._loader._connect_server(server_config)
            except Exception as exc:
                import logging
                logging.getLogger(__name__).error(
                    "add_server %s failed: %s", server_config.name, exc,
                )
                return False
            self._config.servers[server_config.name] = server_config
            self._persist_server(server_config)
            return True

    def _persist_server(self, server_config: McpServerConfig) -> None:
        """写回 YAML（追加到 servers 段，不覆盖现有）。敏感信息转 ${VAR} 占位。"""
        save_mcp_config(self._config)

    def list_servers(self) -> list[dict]:
        """返回已加载 server 列表 + 状态。供 TUI 面板展示。

        返回 [{name, transport, tool_count, health_state}]。

        Returns:
            list[dict]: server 列表（含工具数与健康状态）。
        """
        result: list[dict] = []
        for name, server in self._config.servers.items():
            tool_count = sum(
                1 for entry in self._registry._entries.values()
                if entry.server_name == name
            )
            health = "healthy"
            for entry in self._registry._entries.values():
                if entry.server_name == name and entry.breaker.state != "closed":
                    health = "unhealthy"
                    break
            result.append({
                "name": name,
                "transport": server.transport,
                "tool_count": tool_count,
                "health_state": health,
            })
        return result

    def get_tools(self, groups: list[str]) -> list:
        """按 group 返回工具列表，供 agent 注入。

        Args:
            groups: 工具分组（如 ["core"] / ["core","deferred"]）。

        Returns:
            list: 该分组下的工具列表。
        """
        return self._registry.get_tools_by_group(groups)

    def get_audit_middleware(self) -> McpAuditMiddleware:
        """供 middleware 链注入。"""
        return self._audit

    @property
    def registry(self) -> ToolRegistry:
        """供外化层取 tool_metadata。"""
        return self._registry

    @property
    def loader(self) -> McpLoader:
        """供 reload_mcp_tools 取连接状态。"""
        return self._loader

    async def shutdown(self) -> None:
        """清理所有连接。"""
        await self._loader.shutdown()


def build_mcp_manager(config_path: str | None = None) -> McpManager | None:
    """从 .env 读开关 + YAML 加载。enabled=false 或无配置返 None。

    读 POIROT_MCP_ENABLED（缺省 false）+ POIROT_MCP_CONFIG_PATH。

    Args:
        config_path: 配置路径；None 时走 env 缺省。

    Returns:
        McpManager | None: 启用且有配置时返 McpManager；否则 None。
    """
    enabled = os.environ.get("POIROT_MCP_ENABLED", "false").lower() == "true"
    if not enabled:
        return None
    config = load_mcp_config(config_path)
    if not config.servers:
        return None
    return McpManager(config)