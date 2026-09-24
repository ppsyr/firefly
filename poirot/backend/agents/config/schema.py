"""应用配置数据结构 — frozen dataclass 集合。

【整体职责】
定义应用层的全部配置结构：从运行参数、模型选择、工具、中间件、报告、可观测性，
到上下文治理、沙箱、技能、记忆等子系统的配置引用。以 AppConfig 为根聚合，
供 loader 加载、schema 校验、各模块消费。

【内容摘要】
- RuntimeConfig           : 运行时参数（模式、超时、循环步数、输出目录等）。
- HitlConfig              : Human-in-the-loop 配置（停滞检测、求助阈值等）。
- ContextGovernanceConfig : 上下文治理层配置（策略名 + 参数）。
- ModelConfig             : 模型选择（researcher / reporter）。
- ToolConfig              : 工具配置（搜索 MCP、工具搜索开关）。
- MiddlewareConfig        : 中间件开关集合。
- ReportingConfig         : 报告配置（是否保存 artifact、格式）。
- ObservabilityConfig     : 可观测性配置（事件日志、日志级别）。
- AppConfig               : 应用配置根，聚合以上所有配置及子系统配置。

【职责边界】
- 只负责：定义配置的数据结构、字段语义与默认值。
- 不负责：配置加载与合并（loader）、Schema 校验（schema）、模型路由决策（model_router）、
  各子系统配置的内部语义（MemoryConfig / SandboxConfig / SkillConfig 由各自模块定义）。

【INVARIANT】
- 全部 frozen：构造后不可变，可跨模块 / 跨线程安全共享。
- 无默认值的字段为必填：AppConfig 的 name / environment / runtime / models / tools /
  middleware / reporting / observability，ModelConfig 的 researcher_model / reporter_model。
- 子配置引用默认工厂：AppConfig 的 context_governance / sandbox / skill / memory
  用 field(default_factory=...) 提供默认实例，避免可变默认值陷阱。
- 可变容器用 default_factory：ContextGovernanceConfig.params、MiddlewareConfig.enabled
  等 dict / tuple 字段统一用 default_factory。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from poirot.backend.agents.memory.config import MemoryConfig
from poirot.backend.agents.sandbox.integration.config import SandboxConfig
from poirot.backend.agents.skill.config import SkillConfig


@dataclass(frozen=True)
class RuntimeConfig:
    """运行时参数配置。

    Attributes:
        expert_mode: 是否启用专家模式。
        timezone: 时区。
        max_loop_steps: 单轮最大循环步数。
        timeout_seconds: 运行超时（秒）。
        output_root: 产物输出根目录。
        logs_root: 日志根目录。
        plan_enabled: 是否启用计划。
        reflection_enabled: 是否启用反思。
        graph_node_multiplier: 图节点数倍率（用于预算/上限换算）。
    """

    expert_mode: bool = False
    timezone: str = "Asia/Shanghai"
    max_loop_steps: int = 4
    timeout_seconds: int = 120
    output_root: str = ".poirot"
    logs_root: str = ".poirot/logs"
    plan_enabled: bool = True
    reflection_enabled: bool = False
    graph_node_multiplier: int = 200


@dataclass(frozen=True)
class HitlConfig:
    """Human-in-the-loop 配置：长任务停滞检测与求助。

    Attributes:
        capability_failure_threshold: 能力失败次数阈值，超过触发介入。
        error_pattern_threshold: 错误模式重复次数阈值。
        todo_stagnation_rounds: 待办停滞轮数阈值。
        no_progress_timeout: 无进展超时（秒）。
        max_help_requests: 最大求助次数。
        activity_heartbeat_interval: 活动心跳间隔（秒）。
        cancel_grace_period: 取消宽限期（秒）。
        steer_enabled: 是否启用人工引导。
    """

    capability_failure_threshold: int = 2
    error_pattern_threshold: int = 3
    todo_stagnation_rounds: int = 5
    no_progress_timeout: int = 180
    max_help_requests: int = 3
    activity_heartbeat_interval: int = 10
    cancel_grace_period: int = 5
    steer_enabled: bool = True


@dataclass(frozen=True)
class ContextGovernanceConfig:
    """上下文治理层配置（策略层）。公共层 middleware 固定挂，不经此配置。

    Attributes:
        strategy: 上下文治理策略名。
        params: 策略参数，由具体策略消费。
    """

    strategy: str = "default"
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelConfig:
    """模型选择配置。

    Attributes:
        researcher_model: 研究阶段使用的模型。
        reporter_model: 报告阶段使用的模型。
    """

    researcher_model: str
    reporter_model: str


@dataclass(frozen=True)
class ToolConfig:
    """工具配置。

    Attributes:
        web_search_mcp: Web 搜索 MCP 标识。
        tool_search_default: 是否默认启用工具搜索。
    """

    web_search_mcp: str = "fake"
    tool_search_default: bool = True


@dataclass(frozen=True)
class MiddlewareConfig:
    """中间件开关集合。

    Attributes:
        enabled: 启用的中间件名称集合。
        summarization: 是否启用摘要中间件。
        todo: 是否启用待办中间件。
        title: 是否启用标题中间件。
    """

    enabled: tuple[str, ...] = field(default_factory=tuple)
    summarization: bool = False
    todo: bool = False
    title: bool = False


@dataclass(frozen=True)
class ReportingConfig:
    """报告配置。

    Attributes:
        save_artifact: 是否保存报告 artifact。
        artifact_format: artifact 格式。
    """

    save_artifact: bool = True
    artifact_format: str = "markdown"


@dataclass(frozen=True)
class ObservabilityConfig:
    """可观测性配置。

    Attributes:
        event_log_enabled: 是否启用事件日志。
        log_level: 日志级别。
    """

    event_log_enabled: bool = True
    log_level: str = "INFO"


@dataclass(frozen=True)
class AppConfig:
    """应用配置根，聚合全部配置。

    Attributes:
        name: 应用名称。
        environment: 运行环境（如 dev / prod）。
        runtime: 运行时参数。
        models: 模型选择。
        tools: 工具配置。
        middleware: 中间件开关。
        reporting: 报告配置。
        observability: 可观测性配置。
        context_governance: 上下文治理配置。
        sandbox: 沙箱配置。
        skill: 技能配置。
        memory: 记忆配置。
    """

    name: str
    environment: str
    runtime: RuntimeConfig
    models: ModelConfig
    tools: ToolConfig
    middleware: MiddlewareConfig
    reporting: ReportingConfig
    observability: ObservabilityConfig
    context_governance: ContextGovernanceConfig = field(default_factory=ContextGovernanceConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    skill: SkillConfig = field(default_factory=SkillConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)