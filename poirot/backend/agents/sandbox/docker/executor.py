"""Docker命令执行层——Docker CLI运行的抽象层。

【整体职责】
抽象"docker CLI 在哪里执行 + 宿主路径如何翻译成 daemon 可见路径"。
把容器管理（provisioner / LocalContainerBackend）与命令执行环境（本地 subprocess、
WSL 桥接、未来 SSH / remote）解耦——backend 不关心 docker 命令最终落到哪个环境。

【内容摘要】
- DockerExecutor        : 契约（Protocol）——定义 run / translate_path 两个方法。
- LocalDockerExecutor   : 默认实现——直接 subprocess.run，daemon 与项目同 OS。
- WslDockerExecutor     : WSL 桥接——Windows 项目 + WSL2 内 docker daemon。

【职责边界】
- 只负责：docker CLI 调用的落点选择 + 宿主路径到 daemon 视角的翻译。
- 不负责：docker 命令的拼装（backend 负责）、容器语义（create / inspect 等）。
- 无状态：三个 executor 都不持有可变状态（WSL 版持有固定的 prefix 列表）。

【INVARIANT】
- 契约方法：run(cmd, **kwargs) → subprocess.CompletedProcess；translate_path(host_path) → str。
- run 收到的 cmd 是"裸 docker 参数"（如 ["inspect", "name"]），executor 负责加前缀。
  - Local：直接 subprocess.run(cmd)。
  - WSL：subprocess.run(["wsl", "-d", distro, "--", ...] + cmd)。
- translate_path 语义：把"项目进程视角的宿主路径"翻译成"docker daemon 视角的路径"。
  - Local：恒等（同 OS，视角一致）。
  - WSL：Windows 盘符路径 D:\\foo → /mnt/d/foo。
- DockerExecutor 用 runtime_checkable，支持 isinstance 结构检查。
- kwargs 透传 subprocess.run 的签名（capture_output / text / check / timeout 等）。
"""
from __future__ import annotations

import subprocess
from typing import Protocol, runtime_checkable


@runtime_checkable
class DockerExecutor(Protocol):
    """docker CLI 调用 + 宿主路径翻译的抽象。

    run(): 执行 docker 命令（cmd 是裸 docker 参数，executor 负责加前缀）。
    translate_path(): 把项目进程视角的宿主路径转成 daemon 可见路径。
    """

    def run(self, cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        """执行 docker 命令；kwargs 透传 subprocess.run 签名。"""
        ...

    def translate_path(self, host_path: str) -> str:
        """把宿主路径翻译成 docker daemon 可见路径。"""
        ...


class LocalDockerExecutor:
    """默认 executor：直接 subprocess.run，daemon 与项目同 OS。"""

    def run(self, cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        """直接执行 docker 命令。"""
        return subprocess.run(cmd, **kwargs)

    def translate_path(self, host_path: str) -> str:
        """恒等翻译——同 OS，项目视角与 daemon 视角一致。"""
        return host_path


class WslDockerExecutor:
    """Windows 项目 + WSL2 内 docker daemon 场景的 executor。

    run(): 给 docker 命令加前缀 `wsl -d <distro> [--user <user>] --`。
    translate_path(): Windows 盘符路径 D:\\foo\\bar → /mnt/d/foo/bar。
    """

    def __init__(self, distro: str = "Ubuntu", user: str | None = None) -> None:
        self._prefix = ["wsl", "-d", distro]
        if user:
            self._prefix += ["--user", user]

    def run(self, cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        """加 WSL 前缀后执行——`wsl -d <distro> [--user <user>] -- <cmd>`。"""
        return subprocess.run(self._prefix + ["--"] + cmd, **kwargs)

    def translate_path(self, host_path: str) -> str:
        """Windows 盘符路径 → WSL 挂载路径：D:\\foo → /mnt/d/foo。

        非盘符路径（如已是 /...）原样返回（仅统一分隔符为 /）。
        """
        p = str(host_path).replace("\\", "/")
        if len(p) >= 2 and p[1] == ":":
            drive = p[0].lower()
            return f"/mnt/{drive}{p[2:]}"
        return p