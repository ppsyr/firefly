"""ModelRouter — 角色化智能路由，按 MODEL_ROUTES 链构造 FallbackChatModel，deepseek 兜底。

【整体职责】
模型构造的对外入口：按角色（researcher / reporter / reflection）从可用 provider 中
选出路由链，构造带降级能力的 FallbackChatModel；也支持从指定 provider 开始构造链。

【内容摘要】
- ModelRouter.__init__    : 初始化，接受可选 providers 或自动发现可用 provider。
- ModelRouter.build_model : 按角色路由链构造 FallbackChatModel（多 provider 降级）。
- ModelRouter.build_from: 从指定 provider 开始构造角色降级链。
- ModelRouter.chain_names : 返回某角色的 provider 链名列表。

【职责边界】
- 只负责：角色 → 链的编排，把 chain 中的 provider 逐个实例化并组装为 FallbackChatModel。
- 不负责：provider 配置解析（provider_config）、模型实例化细节（build_chat_model）、
  降级逻辑（FallbackChatModel）、路由表定义（MODEL_ROUTES）。

【INVARIANT】
- 链尾兜底：deepseek 兜尾由 route_chain_for 保证，本类不重复处理。
- providers 可注入：构造时传入 providers 则复用，否则自动 discover_available_providers()，
  便于测试注入。
- build_from 从指定 provider 开始，跳过它之前的 provider，并保留后续降级节点。
"""
from __future__ import annotations

from dataclasses import replace

from langchain_core.language_models import BaseChatModel

from poirot.backend.agents.config.fallback_model import FallbackChatModel
from poirot.backend.agents.config.provider_config import (
    MODEL_ROUTES,
    ProviderConfig,
    ProviderConfigError,
    build_chat_model,
    discover_available_providers,
    route_chain_for,
)
from poirot.backend.agents.config.provider_profile import get_provider_profile


class ModelRouter:
    """角色化智能路由。

    Attributes:
        _providers: 可用 provider 列表；构造时可注入，否则自动发现。
    """

    def __init__(self, providers: list[ProviderConfig] | None = None) -> None:
        """初始化路由。

        Args:
            providers: 可用 provider 列表；为 None 时自动调用 discover_available_providers()。
        """
        self._providers = providers if providers is not None else discover_available_providers()

    def build_model(self, role: str) -> BaseChatModel:
        """按 MODEL_ROUTES[role] 链构造 FallbackChatModel，deepseek 兜尾。

        Args:
            role: 角色名（researcher / reporter / reflection）。

        Returns:
            BaseChatModel: 带降级能力的 FallbackChatModel，链内为各 provider 的 ChatModel 实例。
        """
        chain = route_chain_for(role, self._providers)
        models = [build_chat_model(p) for p in chain]
        return FallbackChatModel(models=models, provider_names=[p.provider for p in chain])

    def build_from(
        self,
        provider: str,
        model: str | None = None,
        role: str = "researcher",
    ) -> FallbackChatModel:
        """从指定 provider 开始构造角色降级链。

        指定 provider 之前的节点会被跳过；命中节点之后的可用 provider
        继续作为 fallback。只有命中节点的模型名会被 ``model`` 覆盖。

        Args:
            provider: 指定的起始 provider 名。
            model: 可选模型名，仅覆盖起始 provider 的默认模型。
            role: 角色名（researcher / reporter / reflection）。

        Returns:
            FallbackChatModel: 从指定 provider 开始的降级链。

        Raises:
            ProviderConfigError: provider 不在角色路由中，或未配置可用凭证。
        """
        route = MODEL_ROUTES.get(role, [])
        if provider not in route:
            raise ProviderConfigError(
                f"provider {provider!r} 不在 {role!r} 路由中，请添加供应商"
            )

        available = {item.provider: item for item in self._providers}
        selected = available.get(provider)
        profile = get_provider_profile(provider)
        if selected is None or (
            not selected.api_key and not (profile and profile.no_key_required)
        ):
            raise ProviderConfigError(
                f"provider {provider!r} 未配置可用 api_key，请配置 apikey"
            )

        start = route.index(provider)
        chain = [available[name] for name in route[start:] if name in available]
        if model:
            chain[0] = replace(chain[0], model=model)

        models = [build_chat_model(config) for config in chain]
        return FallbackChatModel(
            models=models,
            provider_names=[config.provider for config in chain],
        )

    def chain_names(self, role: str) -> list[str]:
        """返回某角色的 provider 链名列表（按路由顺序，含链尾兜底）。

        Args:
            role: 角色名。

        Returns:
            list[str]: provider 名列表。
        """
        chain = route_chain_for(role, self._providers)
        return [p.provider for p in chain]
