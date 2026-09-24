"""配置加载器 — 合并默认配置、叠加层与 CLI 覆盖，校验并构造 AppConfig。

【整体职责】
配置装载的唯一入口：以 DEFAULT_CONFIG 为基线，按 expert_mode 叠加 EXPERT_PROFILE，
再应用 CLI 覆盖，经校验后构造出不可变的 AppConfig。sandbox / memory 的配置从环境变量
懒加载并注入。

【内容摘要】
- ConfigError            : 配置加载或校验失败时抛出。
- load_config            : 主入口，合并 → 校验 → 构造 AppConfig。
- _apply_cli_overrides   : 应用 CLI 覆盖（expert_mode + 扁平字段映射）。
- _validate              : 校验必填字段与取值范围。
- _build_sandbox_config  : 从 POIROT_SANDBOX_* 环境变量构造 SandboxConfig。
- _build_memory_config   : 从 POIROT_MEMORY_* 环境变量构造 MemoryConfig。
- _build_config          : 把 raw dict 组装为 AppConfig。
- _deep_merge            : 递归深合并，dict 逐层覆盖，其他类型直接替换。

【职责边界】
- 只负责：配置的合并、覆盖、校验、组装，以及 sandbox / memory 的 env 懒加载。
- 不负责：默认值的定义（defaults）、配置结构定义（schema）、provider 配置解析
  （provider_config）、sandbox / memory 配置的内部语义（各自模块定义）。

【INVARIANT】
- 不修改全局：先 deepcopy(DEFAULT_CONFIG) 再合并，避免污染默认配置。
- expert_mode 优先级：cli_overrides > 函数参数 > 默认 False。
- expert_mode=True 才叠加 EXPERT_PROFILE；False 时保持 DEFAULT。
- expert_mode 类型强校验：非 bool 抛 ConfigError。
- 扁平 CLI 覆盖：仅支持白名单字段（logs_root / output_root / researcher_model /
  reporter_model / save_artifact），映射到对应 section。
- sandbox / memory 懒加载：use 为空表示禁用，值从环境变量读取。
"""
from __future__ import annotations

import os
from copy import deepcopy
from typing import Any

from poirot.backend.agents.config.defaults import DEFAULT_CONFIG, EXPERT_PROFILE
from poirot.backend.agents.config.schema import (
    AppConfig,
    ContextGovernanceConfig,
    MiddlewareConfig,
    ModelConfig,
    ObservabilityConfig,
    ReportingConfig,
    RuntimeConfig,
    ToolConfig,
)
from poirot.backend.agents.memory.config import MemoryConfig
from poirot.backend.agents.sandbox.integration.config import SandboxConfig


class ConfigError(ValueError):
    """Raised when config cannot be loaded or validated."""


def load_config(
    expert_mode: bool = False,
    cli_overrides: dict[str, Any] | None = None,
) -> AppConfig:
    """加载配置：合并默认 + 叠加层 + CLI 覆盖，校验后构造 AppConfig。

    Args:
        expert_mode: 是否启用 expert 模式（会被 cli_overrides 中的同名项覆盖）。
        cli_overrides: CLI 覆盖项，含 expert_mode 与扁平字段（logs_root 等）。

    Returns:
        AppConfig: 不可变的应用配置。

    Raises:
        ConfigError: 覆盖项类型非法或校验不通过时。
    """
    overrides = cli_overrides or {}
    # expert_mode: cli_overrides 优先，其次参数，最后默认 False
    selected_expert = bool(overrides.get("expert_mode", expert_mode))

    raw = deepcopy(DEFAULT_CONFIG)
    if selected_expert:
        _deep_merge(raw, EXPERT_PROFILE)
    _apply_cli_overrides(raw, overrides)
    _validate(raw)
    return _build_config(raw)


def _apply_cli_overrides(raw: dict[str, Any], overrides: dict[str, Any]) -> None:
    """应用 CLI 覆盖。

    - expert_mode：类型校验后写入 runtime；为 True 时叠加 EXPERT_PROFILE。
    - 扁平字段：按白名单映射到对应 section 写入。

    Args:
        raw: 待修改的配置 dict（原地修改）。
        overrides: CLI 覆盖项。

    Raises:
        ConfigError: expert_mode 非 bool 时。
    """
    if "expert_mode" in overrides:
        em = overrides["expert_mode"]
        if not isinstance(em, bool):
            raise ConfigError("expert_mode must be a boolean")
        raw["runtime"]["expert_mode"] = em
        if em is True:
            _deep_merge(raw, EXPERT_PROFILE)
        # False 时保持 DEFAULT（不 merge EXPERT_PROFILE）

    flat_targets = {
        "logs_root": ("runtime", "logs_root"),
        "output_root": ("runtime", "output_root"),
        "researcher_model": ("models", "researcher_model"),
        "reporter_model": ("models", "reporter_model"),
        "save_artifact": ("reporting", "save_artifact"),
    }
    for key, path in flat_targets.items():
        if key not in overrides:
            continue
        section, field = path
        raw[section][field] = overrides[key]


def _validate(raw: dict[str, Any]) -> None:
    """校验必填字段与取值范围。

    Args:
        raw: 待校验的配置 dict。

    Raises:
        ConfigError: researcher_model / reporter_model 缺失、expert_mode 非 bool、
            max_loop_steps < 1、logs_root 缺失时。
    """
    models = raw["models"]
    if not models.get("researcher_model"):
        raise ConfigError("researcher_model is required")
    if not models.get("reporter_model"):
        raise ConfigError("reporter_model is required")
    if not isinstance(raw["runtime"].get("expert_mode"), bool):
        raise ConfigError("expert_mode must be a boolean")
    if raw["runtime"]["max_loop_steps"] < 1:
        raise ConfigError("max_loop_steps must be greater than zero")
    if not raw["runtime"].get("logs_root"):
        raise ConfigError("logs_root is required")


def _build_sandbox_config() -> SandboxConfig:
    """从 POIROT_SANDBOX_* 环境变量构造 SandboxConfig（懒加载，use 为空=禁用）。

    Returns:
        SandboxConfig: 沙箱配置。
    """
    return SandboxConfig(
        use=os.environ.get("POIROT_SANDBOX_USE", ""),
        allow_host_bash=os.environ.get("POIROT_SANDBOX_ALLOW_HOST_BASH", "true").lower() != "false",
        image=os.environ.get("POIROT_SANDBOX_IMAGE", "all-in-one-sandbox:latest"),
        port=int(os.environ.get("POIROT_SANDBOX_PORT", "18000") or "18000"),
        container_prefix=os.environ.get("POIROT_SANDBOX_CONTAINER_PREFIX", "poirot-sandbox"),
        executor=os.environ.get("POIROT_SANDBOX_EXECUTOR", "local") or "local",  # type: ignore[arg-type]
        wsl_distro=os.environ.get("POIROT_SANDBOX_WSL_DISTRO") or None,
        wsl_user=os.environ.get("POIROT_SANDBOX_WSL_USER") or None,
        idle_timeout=int(os.environ.get("POIROT_SANDBOX_IDLE_TIMEOUT", "600") or "600"),
        replicas=int(os.environ.get("POIROT_SANDBOX_REPLICAS", "3") or "3"),
    )


def _build_memory_config() -> MemoryConfig:
    """从 POIROT_MEMORY_* 环境变量构造 MemoryConfig（懒加载，use 为空=禁用）。

    Returns:
        MemoryConfig: 记忆配置，含 phase2 子配置。
    """
    return MemoryConfig(
        use=os.environ.get("POIROT_MEMORY_USE", ""),
        storage_path=os.environ.get("POIROT_MEMORY_STORAGE_PATH", ".poirot/memory"),
        enable_recall=os.environ.get("POIROT_MEMORY_ENABLE_RECALL", "true").lower() != "false",
        enable_extract=os.environ.get("POIROT_MEMORY_ENABLE_EXTRACT", "false").lower() == "true",
        token_budget=int(os.environ.get("POIROT_MEMORY_TOKEN_BUDGET", "2000") or "2000"),
        phase2={
            "enabled": os.environ.get("POIROT_MEMORY_PHASE2_ENABLED", "false").lower() == "true",
            "trigger_every_n_turns": int(os.environ.get("POIROT_MEMORY_PHASE2_TURNS", "10") or "10"),
            "trigger_on_session_end": True,
        },
    )


def _build_config(raw: dict[str, Any]) -> AppConfig:
    """把 raw dict 组装为 AppConfig。

    Args:
        raw: 已合并、已校验的配置 dict。

    Returns:
        AppConfig: 不可变应用配置，sandbox / memory 由 env 懒加载注入。
    """
    return AppConfig(
        name=raw["name"],
        environment=raw["environment"],
        runtime=RuntimeConfig(**raw["runtime"]),
        models=ModelConfig(**raw["models"]),
        tools=ToolConfig(**raw["tools"]),
        middleware=MiddlewareConfig(**raw["middleware"]),
        reporting=ReportingConfig(**raw["reporting"]),
        observability=ObservabilityConfig(**raw["observability"]),
        context_governance=ContextGovernanceConfig(**raw.get("context_governance", {})),
        sandbox=_build_sandbox_config(),
        memory=_build_memory_config(),
    )


def _deep_merge(target: dict[str, Any], patch: dict[str, Any]) -> None:
    """递归深合并：dict 逐层合并，其他类型直接替换。

    Args:
        target: 目标 dict（原地修改）。
        patch: 覆盖内容。
    """
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value