"""能力注册表 — 运行期能力的统一容器与访问入口。

【整体职责】
集中承载一次运行所需的各类能力对象（模型、工具、reporter、artifact_store、
sandbox_provider、skill_store、specialist_registry、subagent_provider、
memory_provider），并提供按名/按类的 getter。缺失能力时抛 CapabilityMissingError。

【内容摘要】
- CapabilityMissingError  : 所需运行期能力缺失时抛出（继承 KeyError）。
- CapabilityRegistry      : 能力注册表，frozen dataclass，含能力字段与 getter。

【职责边界】
- 只负责：承载能力对象、提供 getter、缺失时抛错。
- 不负责：能力对象的构造与注册（由上层 bootstrap / AppRuntime 装配）、
  能力的实现（reporter / artifact_store / provider 等各自模块）。

【INVARIANT】
- frozen：构造后不可变；能力在构造时一次性注入。
- 缺失即抛错：getter 在能力为 None / 未注册时抛 CapabilityMissingError，
  不返回 None（fail-closed）。
- 两类容器字段：models / tools 为按名索引的 dict；其余为单例字段（Any | None）。
- 默认空容器：models / tools 用 default_factory=dict，避免可变默认值陷阱。
- 错误消息可区分：按名 getter 报 "xxx not registered: {name}"，单例 getter 报 "xxx not registered"。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class CapabilityMissingError(KeyError):
    """Raised when a required runtime capability is missing."""


@dataclass(frozen=True)
class CapabilityRegistry:
    """能力注册表。

    Attributes:
        models: 按名索引的模型集合。
        tools: 按名索引的工具集合。
        reporter: 报告器。
        artifact_store: 产物存储。
        sandbox_provider: 沙箱提供者。
        skill_store: 技能存储。
        specialist_registry: specialist 注册表。
        subagent_provider: 子 Agent 提供者。
        memory_provider: 记忆提供者。
    """

    models: dict[str, Any] = field(default_factory=dict)
    tools: dict[str, Any] = field(default_factory=dict)
    reporter: Any | None = None
    artifact_store: Any | None = None
    sandbox_provider: Any | None = None
    skill_store: Any | None = None
    specialist_registry: Any | None = None
    subagent_provider: Any | None = None
    memory_provider: Any | None = None

    def get_model(self, name: str) -> Any:
        """按名取模型；未注册抛 CapabilityMissingError。

        Args:
            name: 模型名。

        Returns:
            Any: 模型对象。

        Raises:
            CapabilityMissingError: 模型未注册时。
        """
        try:
            return self.models[name]
        except KeyError as exc:
            raise CapabilityMissingError(f"model not registered: {name}") from exc

    def get_tool(self, name: str) -> Any:
        """按名取工具；未注册抛 CapabilityMissingError。

        Args:
            name: 工具名。

        Returns:
            Any: 工具对象。

        Raises:
            CapabilityMissingError: 工具未注册时。
        """
        try:
            return self.tools[name]
        except KeyError as exc:
            raise CapabilityMissingError(f"tool not registered: {name}") from exc

    def get_reporter(self) -> Any:
        """取报告器；未注册抛 CapabilityMissingError。"""
        if self.reporter is None:
            raise CapabilityMissingError("reporter not registered")
        return self.reporter

    def get_artifact_store(self) -> Any:
        """取产物存储；未注册抛 CapabilityMissingError。"""
        if self.artifact_store is None:
            raise CapabilityMissingError("artifact_store not registered")
        return self.artifact_store

    def get_sandbox_provider(self) -> Any:
        """取沙箱提供者；未注册抛 CapabilityMissingError。"""
        if self.sandbox_provider is None:
            raise CapabilityMissingError("sandbox_provider not registered")
        return self.sandbox_provider

    def get_skill_store(self) -> Any:
        """取技能存储；未注册抛 CapabilityMissingError。"""
        if self.skill_store is None:
            raise CapabilityMissingError("skill_store not registered")
        return self.skill_store

    def get_specialist_registry(self) -> Any:
        """取 specialist 注册表；未注册抛 CapabilityMissingError。"""
        if self.specialist_registry is None:
            raise CapabilityMissingError("specialist_registry not registered")
        return self.specialist_registry

    def get_subagent_provider(self) -> Any:
        """取子 Agent 提供者；未注册抛 CapabilityMissingError。"""
        if self.subagent_provider is None:
            raise CapabilityMissingError("subagent_provider not registered")
        return self.subagent_provider

    def get_memory_provider(self) -> Any:
        """取记忆提供者；未注册抛 CapabilityMissingError。"""
        if self.memory_provider is None:
            raise CapabilityMissingError("memory_provider not registered")
        return self.memory_provider