"""LocalContainerBackend — 本地容器基础设施 CRUD（Docker / Apple Container CLI）。

【整体职责】
实现 SandboxBackend 契约的"本地容器版本"：用 docker / Apple Container CLI 管理
沙箱容器的创建 / 发现 / 存活检查 / 批量列举 / 销毁，以及端口分配与跨进程发现。
不依赖 Docker SDK，直接用 subprocess 调 CLI，减少第三方依赖。
被 DockerSandboxProvider 用作底层基础设施层。

【内容摘要】
- 模块常量：
    _DEFAULT_IMAGE                 : 默认镜像（all-in-one-sandbox:latest）。
    _CONTAINER_PORT                : 容器内服务端口（8080）。
    _VIRTUAL_PATH_PREFIX           : 容器内虚拟路径前缀（/mnt/poirot/user-data）。
    _MAX_PORT_RETRIES              : 端口重试上限（10）。
    _DOCKER_TIMESTAMP_SENTINEL     : 时间戳解析失败时的哨兵值（0.0）。
- 模块函数：
    _parse_docker_timestamp        : 解析 Docker ISO 8601 时间戳（纳秒 + Z）。
    _extract_host_port             : 从 docker inspect 条目提 host port。
    _format_mount                  : 格式化 bind mount（Docker --mount / Apple -v）。
    _get_free_port                 : bind 探测空闲端口。
    _is_no_such_container_error    : 判定 stderr 是否明确"容器不存在"。
- LocalContainerBackend            : 本地容器后端类。
    ├─ __init__ / _detect_runtime / _container_name
    ├─ create          : 幂等创建容器（端口重试循环）。
    ├─ _start_container: 拼 docker run 命令并执行。
    ├─ discover        : 按确定性 ID 查已有容器（跨进程恢复）。
    ├─ _get_container_port: docker port 查 host port。
    ├─ destroy         : 幂等静默停止容器（--rm 自动移除）。
    ├─ is_alive        : 轻量 docker inspect，返 True / False / None。
    ├─ list_running    : batch docker inspect（2 次 subprocess，非 N+1）。
    └─ _batch_inspect  : 单次 docker inspect *names → dict。

【职责边界】
- 只负责：容器基础设施 CRUD（create / discover / destroy / is_alive / list_running）。
- 不负责：容器内命令执行（DockerRuntime）、生命周期编排 / 缓存 / idle（provider）、
  路径翻译（translator）、安全校验（guard）。
- 持有状态：镜像 / 端口 / 前缀 / sandbox_root / environment / executor / runtime 类型。

【INVARIANT】
- create 幂等：同 sandbox_id 已存在则返回既有实例。
- 容器命名 {container_prefix}-{sandbox_id}（确定性，跨进程可发现）。
- bind mount .poirot/sandbox/{sandbox_id}:/mnt/poirot/user-data（host 可见）。
- is_alive 轻量（docker inspect），不调 HTTP；返回 None 表示"未知"（不误杀）。
- 端口 retry loop 10 次（bind→release→run 竞态兜底）。
- --rm：容器停止自动移除，destroy 幂等静默（不调 docker rm）。
- list_running batch docker inspect（2 次 subprocess，非 N+1）。
- env vars 传 SANDBOX_ID + THREAD_ID。
- Docker 用 --mount type=bind（避免 Windows 盘符 : 歧义）；Apple Container 用 -v。
- macOS 优先 Apple Container，否则 Docker。
- _is_no_such_container_error 区分"容器不存在"与 transient 错误。
"""
from __future__ import annotations

import json
import logging
import os
import platform
import shlex
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from poirot.backend.agents.journal.events import utc_now_iso
from poirot.backend.agents.sandbox.contracts import SandboxBackend
from poirot.backend.agents.sandbox.docker.executor import (
    DockerExecutor,
    LocalDockerExecutor,
)
from poirot.backend.agents.sandbox.types import PathMapping, SandboxInfo
from poirot.backend.agents.sandbox.utils.sandbox_id import validate_sandbox_id

logger = logging.getLogger(__name__)

_DEFAULT_IMAGE = "all-in-one-sandbox:latest"
_CONTAINER_PORT = 8080
_VIRTUAL_PATH_PREFIX = "/mnt/poirot/user-data"
_MAX_PORT_RETRIES = 10
_DOCKER_TIMESTAMP_SENTINEL = 0.0


def _parse_docker_timestamp(raw: str) -> float:
    """解析 Docker ISO 8601 时间戳（纳秒精度 + Z 后缀）→ epoch float；失败返 0.0。"""
    if not raw:
        return _DOCKER_TIMESTAMP_SENTINEL
    try:
        s = raw.strip()
        if "." in s:
            dot = s.index(".")
            tz_start = dot + 1
            while tz_start < len(s) and s[tz_start].isdigit():
                tz_start += 1
            frac = s[dot + 1 : tz_start][:6]
            s = s[: dot + 1] + frac + s[tz_start:]
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return _DOCKER_TIMESTAMP_SENTINEL


def _extract_host_port(inspect_entry: dict, container_port: int) -> int | None:
    """从 docker inspect 条目提取映射到 container_port/tcp 的 host port。"""
    try:
        ports = (inspect_entry.get("NetworkSettings") or {}).get("Ports") or {}
        bindings = ports.get(f"{container_port}/tcp") or []
        if bindings:
            host_port = bindings[0].get("HostPort")
            if host_port:
                return int(host_port)
    except (ValueError, TypeError, AttributeError):
        pass
    return None


def _format_mount(runtime: str, host_path: str, container_path: str, read_only: bool) -> list[str]:
    """格式化 bind mount。

    - Docker 用 --mount type=bind（避免 Windows 盘符 : 歧义）。
    - Apple Container 用 -v。
    """
    if runtime == "docker":
        spec = f"type=bind,src={host_path},dst={container_path}"
        if read_only:
            spec += ",readonly"
        return ["--mount", spec]
    spec = f"{host_path}:{container_path}"
    if read_only:
        spec += ":ro"
    return ["-v", spec]


def _get_free_port(start_port: int) -> int:
    """从 start_port 起 bind 探测空闲端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", start_port))
        return s.getsockname()[1]


def _is_no_such_container_error(stderr: str, container_name: str) -> bool:
    """判定 stderr 是否明确说"容器不存在"（区分 transient 错误）。"""
    msg = stderr.lower()
    if "no such object" in msg or "no such container" in msg:
        return True
    if "not found" not in msg:
        return False
    return container_name.lower() in msg or "container" in msg or "object" in msg


class LocalContainerBackend(SandboxBackend):
    """本地容器后端：Docker / Apple Container CLI CRUD。

    实现 SandboxBackend ABC。用 docker CLI 创建 / 销毁 / 检查容器。
    不依赖 Docker SDK，减少第三方依赖。

    核心约束见模块级 INVARIANT。
    """

    def __init__(
        self,
        image: str = _DEFAULT_IMAGE,
        base_port: int = 8080,
        container_prefix: str = "poirot-sandbox",
        sandbox_root: str | None = None,
        environment: dict[str, str] | None = None,
        executor: DockerExecutor | None = None,
    ) -> None:
        self._image = image
        self._base_port = base_port
        self._prefix = container_prefix
        self._sandbox_root = Path(sandbox_root) if sandbox_root else Path.cwd() / ".poirot" / "sandbox"
        self._environment = environment or {}
        self._executor = executor or LocalDockerExecutor()
        self._runtime = self._detect_runtime()

    def _detect_runtime(self) -> str:
        """探测容器运行时：macOS 优先 Apple Container，否则 Docker。"""
        if platform.system() == "Darwin":
            try:
                subprocess.run(
                    ["container", "--version"], capture_output=True, text=True, check=True, timeout=5,
                )
                logger.info("Detected Apple Container")
                return "container"
            except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                logger.info("Apple Container not available, falling back to Docker")
        return "docker"

    def _container_name(self, sandbox_id: str) -> str:
        """容器名 = {prefix}-{sandbox_id}（确定性，跨进程可发现）。"""
        return f"{self._prefix}-{sandbox_id}"

    def create(
        self,
        thread_id: str,
        sandbox_id: str,
        extra_mounts: list[PathMapping] | None = None,
        *,
        user_id: str | None = None,
    ) -> SandboxInfo:
        """创建容器（幂等）：已存在则复用；否则带端口重试循环启动。

        - 端口冲突（already allocated / in use）→ 换端口重试，最多 10 次。
        - 容器名冲突 → discover 复用已存在容器。
        """
        validate_sandbox_id(sandbox_id)
        name = self._container_name(sandbox_id)
        existing = self.discover(sandbox_id)
        if existing is not None:
            logger.info(f"Reusing existing container {name}")
            return existing

        next_start = self._base_port
        for _attempt in range(_MAX_PORT_RETRIES):
            port = _get_free_port(next_start)
            try:
                container_id = self._start_container(name, port, sandbox_id, thread_id, extra_mounts)
                sandbox_host = os.environ.get("POIROT_SANDBOX_HOST", "localhost")
                logger.info(f"Started container {name} on port {port}")
                return SandboxInfo(
                    sandbox_id=sandbox_id,
                    sandbox_url=f"http://{sandbox_host}:{port}",
                    container_name=name,
                    container_id=container_id,
                )
            except RuntimeError as exc:
                err = str(exc).lower()
                if "port is already allocated" in err or "address already in use" in err:
                    logger.warning(f"Port {port} rejected by Docker, retrying")
                    next_start = port + 1
                    continue
                if "is already in use by container" in err or "conflict. the container name" in err:
                    logger.warning(f"Container name {name} conflict, discovering existing")
                    existing = self.discover(sandbox_id)
                    if existing is not None:
                        return existing
                raise
        raise RuntimeError("Could not start sandbox container: all candidate ports are already allocated by Docker")

    def _start_container(
        self,
        name: str,
        port: int,
        sandbox_id: str,
        thread_id: str,
        extra_mounts: list[PathMapping] | None,
    ) -> str:
        """拼 docker run 命令并执行，返回 container_id。

        - Docker 加 --security-opt seccomp=unconfined + bind_host 限制。
        - 注入 SANDBOX_ID / THREAD_ID + 自定义 environment。
        - bind mount sandbox_root/{sandbox_id} → /mnt/poirot/user-data。
        - 追加 extra_mounts。
        """
        cmd = [self._runtime, "run"]
        if self._runtime == "docker":
            cmd.extend(["--security-opt", "seccomp=unconfined"])
            bind_host = os.environ.get("POIROT_SANDBOX_BIND_HOST", "127.0.0.1")
            port_mapping = f"{bind_host}:{port}:{_CONTAINER_PORT}"
        else:
            port_mapping = f"{port}:{_CONTAINER_PORT}"

        cmd.extend(["--rm", "-d", "-p", port_mapping, "--name", name])

        cmd.extend(["-e", f"SANDBOX_ID={sandbox_id}", "-e", f"THREAD_ID={thread_id}"])
        for key, value in self._environment.items():
            cmd.extend(["-e", f"{key}={value}"])

        host_data = str(self._sandbox_root / sandbox_id)
        Path(host_data).mkdir(parents=True, exist_ok=True)
        cmd.extend(_format_mount(
            self._runtime, self._executor.translate_path(host_data), _VIRTUAL_PATH_PREFIX, False,
        ))

        for mount in (extra_mounts or []):
            cmd.extend(_format_mount(
                self._runtime,
                self._executor.translate_path(mount.local_path),
                mount.container_path, mount.read_only,
            ))

        cmd.append(self._image)
        try:
            result = self._executor.run(cmd, capture_output=True, text=True, check=True)
            return result.stdout.strip()
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"Failed to start sandbox container: {exc.stderr}") from exc

    def discover(self, sandbox_id: str) -> SandboxInfo | None:
        """按确定性 ID 查已有实例（跨进程恢复用）。

        容器不存在 / 未运行 / 端口查不到 → 返回 None。
        """
        validate_sandbox_id(sandbox_id)
        name = self._container_name(sandbox_id)
        try:
            result = self._executor.run(
                [self._runtime, "inspect", "-f", "{{.State.Running}}", name],
                capture_output=True, text=True, timeout=5,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None
        if result.returncode != 0:
            return None
        if result.stdout.strip().lower() != "true":
            return None
        port = self._get_container_port(name)
        if port is None:
            return None
        sandbox_host = os.environ.get("POIROT_SANDBOX_HOST", "localhost")
        return SandboxInfo(
            sandbox_id=sandbox_id,
            sandbox_url=f"http://{sandbox_host}:{port}",
            container_name=name,
        )

    def _get_container_port(self, name: str) -> int | None:
        """docker port {name} 8080 → host port。"""
        try:
            result = self._executor.run(
                [self._runtime, "port", name, str(_CONTAINER_PORT)],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return int(result.stdout.strip().split(":")[-1])
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
            pass
        return None

    # ── 查询 / 销毁 ──

    def destroy(self, info: SandboxInfo) -> None:
        """幂等静默停止容器。--rm 保证容器停止后自动移除，不调 docker rm。"""
        target = info.container_id or info.container_name
        if not target:
            return
        try:
            self._executor.run(
                [self._runtime, "stop", target],
                capture_output=True, text=True, check=True, timeout=15,
            )
            logger.info(f"Stopped container {target}")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            logger.warning(f"Failed to stop container {target}: {exc}")

    def is_alive(self, info: SandboxInfo) -> bool | None:
        """轻量 docker inspect 判断存活；不调 HTTP。

        返回 True（运行）/ False（容器不存在）/ None（未知，不误杀）。
        """
        name = info.container_name or self._container_name(info.sandbox_id)
        if not name:
            return None
        try:
            result = self._executor.run(
                [self._runtime, "inspect", "-f", "{{.State.Running}}", name],
                capture_output=True, text=True, timeout=5,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None
        if result.returncode == 0:
            return result.stdout.strip().lower() == "true"
        if _is_no_such_container_error(result.stderr, name):
            return False
        return None

    def list_running(self) -> list[SandboxInfo]:
        """batch docker inspect（2 次 subprocess，非 N+1）。孤儿对账用。

        - docker ps --filter name={prefix}- 拿容器名。
        - 单次 docker inspect *names 拿 created_at + host_port。
        """
        try:
            result = self._executor.run(
                [self._runtime, "ps", "--filter", f"name={self._prefix}-",
                 "--format", "{{.Names}}"],
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return []
        if result.returncode != 0 or not result.stdout.strip():
            return []

        names = [n.strip() for n in result.stdout.strip().splitlines()
                 if n.strip().startswith(f"{self._prefix}-")]
        if not names:
            return []

        inspections = self._batch_inspect(names)
        sandbox_host = os.environ.get("POIROT_SANDBOX_HOST", "localhost")
        infos: list[SandboxInfo] = []
        for name in names:
            data = inspections.get(name)
            if data is None:
                continue
            created_at, host_port = data
            sid = name[len(self._prefix) + 1:]
            created_iso = (
                datetime.fromtimestamp(created_at, tz=timezone.utc).isoformat()
                if created_at > 0 else utc_now_iso()
            )
            url = f"http://{sandbox_host}:{host_port}" if host_port else ""
            infos.append(SandboxInfo(
                sandbox_id=sid, sandbox_url=url, container_name=name, created_at=created_iso,
            ))
        logger.info(f"Found {len(infos)} running sandbox container(s)")
        return infos

    def _batch_inspect(self, names: list[str]) -> dict[str, tuple[float, int | None]]:
        """单次 docker inspect *names → {name: (created_at_epoch, host_port)}。"""
        if not names:
            return {}
        try:
            result = self._executor.run(
                [self._runtime, "inspect", *names],
                capture_output=True, text=True, timeout=15,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return {}
        if result.returncode != 0:
            return {}
        try:
            payload = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return {}
        out: dict[str, tuple[float, int | None]] = {}
        for entry in payload:
            name = (entry.get("Name") or "").lstrip("/")
            if not name:
                continue
            created = _parse_docker_timestamp(entry.get("Created", ""))
            host_port = _extract_host_port(entry, _CONTAINER_PORT)
            out[name] = (created, host_port)
        return out