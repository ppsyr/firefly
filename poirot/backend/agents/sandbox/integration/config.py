"""Sandbox 配置定义 — 沙箱层的 startup-only 配置结构。

【整体职责】
定义 sandbox 层的配置数据结构（SandboxConfig / SandboxMountConfig），
供 integration / provider / middleware 等模块读取，决定沙箱是否启用、
以什么方式启用、挂载哪些目录、注入哪些环境变量。

【内容摘要】
- SandboxMountConfig : 自定义 bind mount 配置（宿主路径 → 容器路径 + 只读标记）。
- SandboxConfig      : Sandbox 主配置，含启用开关、本地字段、Docker 预留字段、executor 选择。
- STARTUP_ONLY_FIELDS: 标记哪些配置字段属于 startup-only（当前仅 "sandbox"）。

【职责边界】
- 只负责：定义配置字段、字段语义、默认值。
- 不负责：配置加载 / 校验（由 loader 负责）、provider 创建（由各自 provider 负责）、
  runtime 行为（由 runtime 实现负责）。
- 不持有运行时状态：纯配置定义，无副作用。

【INVARIANT】
- 两个 dataclass 均 frozen=True：配置不可变，保证线程安全、可安全共享。
- 未配置（use 为空）时 sandbox 系统不启用。
- Docker 专属字段（image / port / container_prefix / idle_timeout / replicas /
  provisioner_url）为预留字段（Stage 5），当前不生效。
- STARTUP_ONLY_FIELDS 表示这些字段变更需重启才生效。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class SandboxMountConfig:
    """自定义 bind mount 配置（宿主路径 → 容器路径 + 只读标记）。"""

    host_path: str
    container_path: str
    read_only: bool = False


@dataclass(frozen=True)
class SandboxConfig:
    """Sandbox 主配置（startup-only：变更需重启）。

    未配置（use 为空）时 sandbox 系统不启用。
    Docker 专属字段（image / port / container_prefix / idle_timeout / replicas /
    provisioner_url）预留（Stage 5）。
    """

    use: str = ""
    allow_host_bash: bool = True
    storage_path: str = ""
    mounts: list[SandboxMountConfig] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    image: str = ""
    port: int = 8080
    container_prefix: str = "poirot-sandbox"
    idle_timeout: int = 600
    replicas: int = 3
    provisioner_url: str = ""
    executor: Literal["local", "wsl"] = "local"
    wsl_distro: str | None = None
    wsl_user: str | None = None


STARTUP_ONLY_FIELDS: set[str] = {"sandbox"}