"""Capability registry and adapters — 能力注册表与适配器模块。

【整体职责】
集中承载一次运行所需的各类能力对象，并提供统一访问入口。上层（bootstrap / AppRuntime）
在构造时注入能力，各组件（middleware / thread_report / multiagent / memory / skill）
经 getter 按需获取，缺失即 fail-closed 抛错。

【内容摘要】
- registry   : 能力注册表（CapabilityRegistry / CapabilityMissingError）。
- models     : 能力模型目录（预留，暂无实现）。
- adapters   : 能力适配器（预留，暂无实现）。

【职责边界】
- 只负责：承载能力对象、提供 getter、缺失时抛错。
- 不负责：能力对象的构造与注册（由上层 bootstrap / AppRuntime 装配）、
  能力的实现（reporter / artifact_store / provider 等各自模块）、
  适配逻辑（adapters 待实现）。

【能力清单】
- 按名索引：models / tools（dict）。
- 单例能力：reporter / artifact_store / sandbox_provider / skill_store /
  specialist_registry / subagent_provider / memory_provider。

【INVARIANT】
- frozen：注册表构造后不可变，能力在构造时一次性注入。
- fail-closed：getter 在能力缺失时抛 CapabilityMissingError，不返回 None。
"""
from poirot.backend.agents.capabilities.registry import (
    CapabilityMissingError,
    CapabilityRegistry,
)

__all__ = ["CapabilityMissingError", "CapabilityRegistry"]