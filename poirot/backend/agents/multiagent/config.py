"""MultiAgentConfig — 多 Agent 编排配置。

【整体职责】
定义 multiagent 层的全部配置结构，并从环境变量（POIROT_MULTIAGENT_*）加载。
配置覆盖四个层面：编排主配置（MultiAgentConfig）、L2 进化层（L2Config）、
L3 评估层（L3Config）、预算控制（BudgetConfig / SpecialistBudgetLimit）。

【内容摘要】
- L2Config                  : L2 进化层配置（触发周期、冷却、退化阈值、成本/延迟告警等）。
- L3Config                  : L3 评估层配置（评估方法、LLM 评判权重、健康检查窗口等）。
- SpecialistBudgetLimit     : 单个 specialist 的每日预算上限。
- BudgetConfig              : 各 specialist 的预算上限集合 + 全局告警阈值。
- MultiAgentConfig          : 编排主配置（启用开关、specialist 列表、并发/超时、Pi 专属配置）。
- STARTUP_ONLY_FIELDS       : 标记启动时确定、不可热切换的字段集合。
- load_multiagent_config()  : 从环境变量构造 MultiAgentConfig 的唯一入口。

【职责边界】
- 只负责：定义配置结构、字段默认值、从 env 加载、标记 startup-only 字段。
- 不负责：配置校验（由 loader 层负责）、配置热更新、运行时状态管理。
- 不持有运行时状态：全部为 frozen dataclass，构造后不可变。

【INVARIANT】
- 全部配置类 frozen：保证线程安全，可安全跨 Agent / 跨线程共享。
- MultiAgentConfig.enabled 默认 True（default + expert 模式都装配 multiagent）；
  与早期"opt-in 默认关闭"的注释相反，以代码为准。
- L2 / L3 默认 enabled=False：数据驱动触发，不默认开启。
- STARTUP_ONLY_FIELDS 中的字段一旦启动不可改，热更新时须忽略。
- 所有环境变量读取均带默认值兜底：类型转换失败（ValueError）时回退默认，不抛异常。
- 空字符串环境变量（如 Pi 的 provider / api_key / model）表示"未配置"，由下游判空。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class L2Config:
    """L2 进化层配置。

    enabled=False 默认——数据驱动触发，不默认开启。
    涵盖：触发周期、冷却、反循环窗口、失败统计、退化检测、成本/延迟告警、
    进化模型、评估采样、意图识别等。
    """

    enabled: bool = False
    cron_interval_hours: float = 6.0
    cooldown_seconds: float = 3600.0
    anti_loop_window: int = 5
    failure_window_hours: float = 24.0
    failure_threshold: int = 5
    degradation_min_invoked: int = 5
    degradation_threshold: float = 0.4
    cost_alert_usd: float = 1.0
    latency_alert_seconds: float = 300.0
    blocked_auto_release_hours: float = 24.0
    evolution_model: str | None = None
    eval_timeout_seconds: float = 1800.0
    eval_sample_min: int = 10
    eval_sample_max: int = 15
    eval_task_max_reuse: int = 3
    intent_llm_enabled: bool = False
    intent_model: str | None = None
    intent_confidence_threshold: float = 0.7
    intent_delegate_rate_threshold: float = 0.2
    intent_ability_failure_threshold: float = 0.5
    intent_metadata_sample_size: int = 20
    l3_enabled: bool = False


@dataclass(frozen=True)
class L3Config:
    """L3 评估层配置。

    enabled=False 默认——数据驱动触发，不默认开启。
    llm_judge_weights 复用 skill TaskQualityJudge 的权重值。
    """

    enabled: bool = False
    default_eval_method: str = "programmatic"
    llm_judge_model: str | None = None
    llm_judge_weights: dict = field(
        default_factory=lambda: {
            "task_completion": 0.50,
            "response_quality": 0.35,
            "efficiency": 0.05,
            "tool_usage": 0.10,
        }
    )
    health_check_window: int = 20
    degradation_threshold: float = 0.4
    degradation_delta: float = 0.15
    decision_log_retention_days: int = 90
    decision_log_archive_enabled: bool = True


@dataclass(frozen=True)
class SpecialistBudgetLimit:
    """单个 specialist 的每日预算上限。

    三个维度：token 数、成本（美元）、调用次数。
    """

    per_day_tokens: int = 200000
    per_day_cost_usd: float = 20.0
    per_day_calls: int = 50


@dataclass(frozen=True)
class BudgetConfig:
    """预算配置。

    warning_threshold=0.8：使用量达到上限的 80% 时告警。
    """

    codex: SpecialistBudgetLimit = field(default_factory=SpecialistBudgetLimit)
    claude: SpecialistBudgetLimit = field(default_factory=SpecialistBudgetLimit)
    subagent: SpecialistBudgetLimit = field(default_factory=SpecialistBudgetLimit)
    pi: SpecialistBudgetLimit = field(default_factory=SpecialistBudgetLimit)
    warning_threshold: float = 0.8


@dataclass(frozen=True)
class MultiAgentConfig:
    """多 Agent 编排主配置。

    enabled=True 默认——default + expert 模式都装配 multiagent。
    可用 POIROT_MULTIAGENT_ENABLED=false 显式关闭。

    字段分组：
    - 编排主控：enabled / specialists_use / auto_approve / max_concurrent /
      timeout_seconds / max_steps。
    - subagent 专属：subagent_tool_groups / subagent_max_steps /
      subagent_timeout_seconds。
    - 指标与健康：metrics_db_path / metrics_health_threshold / metrics_min_invoked。
    - Pi 专属：specialists_pi_* 系列。
    - 子层配置：l2 / budget / l3。
    """

    enabled: bool = True
    specialists_use: tuple[str, ...] = ("pi", "codex", "claude", "subagent")
    auto_approve: bool = True
    max_concurrent: int = 1
    timeout_seconds: int = 600
    max_steps: int = 50
    subagent_tool_groups: tuple[str, ...] = ("core",)
    subagent_max_steps: int = 20
    subagent_timeout_seconds: int = 300
    metrics_db_path: str = ".poirot/multiagent.db"
    metrics_health_threshold: float = 0.4
    metrics_min_invoked: int = 5
    # Pi specialist 配置
    specialists_pi_provider: str = ""
    specialists_pi_api_key: str = ""
    specialists_pi_auto_install: bool = True
    specialists_pi_model: str = ""
    specialists_pi_thinking_level: str = "medium"
    # L2 进化层配置（默认关闭，数据驱动触发）
    l2: L2Config = field(default_factory=L2Config)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    # L3 评估层配置（默认关闭，数据驱动触发）
    l3: L3Config = field(default_factory=L3Config)


# 启动时确定、不可热切换的字段。热更新配置时须忽略这些字段。
STARTUP_ONLY_FIELDS = frozenset({
    "enabled",
    "specialists_use",
    "metrics_db_path",
    "l2.enabled",
    "l3.enabled",
})


def _env_bool(name: str, default: bool = False) -> bool:
    """读取布尔型环境变量；未设置或值不在白名单内时回退默认。"""
    val = os.getenv(name)
    if val is None:
        return default
    return val.lower() in ("true", "1", "yes")


def _env_int(name: str, default: int) -> int:
    """读取整型环境变量；转换失败时回退默认，不抛异常。"""
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    """读取浮点型环境变量；转换失败时回退默认，不抛异常。"""
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_tuple(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """读取逗号分隔的元组型环境变量；空值或全空白时回退默认。"""
    val = os.getenv(name, "")
    if not val:
        return default
    return tuple(s.strip() for s in val.split(",") if s.strip())


def load_multiagent_config() -> MultiAgentConfig:
    """从环境变量（POIROT_MULTIAGENT_*）加载 multiagent 配置。

    未设置的字段一律走默认值；类型转换失败也回退默认。
    这是构造 MultiAgentConfig 的唯一入口。
    """
    return MultiAgentConfig(
        enabled=_env_bool("POIROT_MULTIAGENT_ENABLED", True),
        specialists_use=_env_tuple("POIROT_MULTIAGENT_SPECIALISTS", ("pi", "codex", "claude", "subagent")),
        auto_approve=_env_bool("POIROT_MULTIAGENT_AUTO_APPROVE", True),
        max_concurrent=_env_int("POIROT_MULTIAGENT_MAX_CONCURRENT", 1),
        timeout_seconds=_env_int("POIROT_MULTIAGENT_TIMEOUT", 600),
        max_steps=_env_int("POIROT_MULTIAGENT_MAX_STEPS", 50),
        subagent_tool_groups=_env_tuple("POIROT_MULTIAGENT_SUBAGENT_TOOL_GROUPS", ("core",)),
        subagent_max_steps=_env_int("POIROT_MULTIAGENT_SUBAGENT_MAX_STEPS", 20),
        subagent_timeout_seconds=_env_int("POIROT_MULTIAGENT_SUBAGENT_TIMEOUT", 300),
        metrics_db_path=os.getenv("POIROT_MULTIAGENT_DB_PATH", ".poirot/multiagent.db"),
        metrics_health_threshold=_env_float("POIROT_MULTIAGENT_HEALTH_THRESHOLD", 0.4),
        metrics_min_invoked=_env_int("POIROT_MULTIAGENT_MIN_INVOKED", 5),
        # Pi specialist 配置
        specialists_pi_provider=os.getenv("POIROT_MULTIAGENT_PI_PROVIDER", ""),
        specialists_pi_api_key=os.getenv("POIROT_MULTIAGENT_PI_API_KEY", ""),
        specialists_pi_auto_install=_env_bool("POIROT_MULTIAGENT_PI_AUTO_INSTALL", True),
        specialists_pi_model=os.getenv("POIROT_MULTIAGENT_PI_MODEL", ""),
        specialists_pi_thinking_level=os.getenv("POIROT_MULTIAGENT_PI_THINKING", "medium"),
        l2=L2Config(
            enabled=_env_bool("POIROT_MULTIAGENT_L2_ENABLED", False),
            cron_interval_hours=_env_float("POIROT_MULTIAGENT_L2_CRON_HOURS", 6.0),
            cooldown_seconds=_env_float("POIROT_MULTIAGENT_L2_COOLDOWN", 3600.0),
            evolution_model=os.getenv("POIROT_MULTIAGENT_L2_MODEL") or None,
            l3_enabled=_env_bool("POIROT_MULTIAGENT_L3_ENABLED", False),
        ),
        l3=L3Config(
            enabled=_env_bool("POIROT_MULTIAGENT_L3_ENABLED", False),
            llm_judge_model=os.getenv("POIROT_MULTIAGENT_L3_JUDGE_MODEL") or None,
        ),
    )