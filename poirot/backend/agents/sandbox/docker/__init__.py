"""Sandbox Docker 容器隔离层 — Docker 实现组件的统一出口。

【整体职责】
汇总 sandbox 层的 Docker 实现组件，对外统一暴露入口。调用方（provider / 装配层）
从本包导入 Docker 相关实现，无需关心具体实现来自哪个文件。

【内容摘要】
- docker_sandbox_provider : DockerSandboxProvider —— 容器生命周期编排（三层缓存 + warm_pool）。
- local_container_backend : LocalContainerBackend —— 容器基础设施 CRUD（docker CLI）。
- remote_container_backend: RemoteContainerBackend —— 远程 / K8s 容器后端。
- executor                : DockerExecutor 契约 + Local / Wsl 两个实现。
- readiness               : 就绪轮询（sync + async）。
- cross_process_lock      : 跨进程文件锁（fcntl / msvcrt）。

【职责边界】
- 只负责：汇总并导出 Docker 实现组件（当前 __init__ 仅导出 executor 三件）。
- 不负责：具体实现逻辑（由各文件负责）、契约定义
  （contracts/sandbox_backend.py、contracts/sandbox_provider.py）。

【INVARIANT】
- DockerRuntime 不在本包导出——它在 runtimes/ 下，与 local_runtime 并列。
- 本文件只做 re-export，不含任何逻辑。
"""
from poirot.backend.agents.sandbox.docker.executor import (
    DockerExecutor,
    LocalDockerExecutor,
    WslDockerExecutor,
)

__all__ = ["DockerExecutor", "LocalDockerExecutor", "WslDockerExecutor"]