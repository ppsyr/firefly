"""DockerPathGuard — Docker 路径白名单守卫（写入必须落挂载区）。

【整体职责】
实现 security_guard 契约的"Docker 路径"版本：约束**写入**操作只能落在挂载区
（/mnt/poirot/user-data/），包括两条路径——
① validate_path(write=True)：直接写入的路径；
② validate_command：bash 重定向目标（> / >>）的绝对路径。
读操作不限制——容器边界本身已提供隔离，读越界危害有限。

【内容摘要】
- _VIRTUAL_PREFIX   : 唯一允许写入的虚拟路径前缀（/mnt/poirot/user-data/）。
- _REDIRECT_PATTERN : 捕获 bash 重定向目标绝对路径的正则（> 或 >>）。
- DockerPathGuard   : Docker 路径守卫类。
    ├─ validate_path    : 写入路径校验（write=True 时才查前缀）。
    └─ validate_command : 命令中重定向目标校验。

【职责边界】
- 只负责：写入路径的前缀白名单校验（直接路径 + 重定向目标）。
- 不负责：读路径校验（读不限制）、路径转换（translator）、
  相对路径解析 / cd 后路径解析（容器隔离兜底）。
- 无状态：不持有任何字段，规则全在模块级常量里。

【INVARIANT】
- 只约束写入，不约束读取：validate_path 在 write=False 时直接返回。
- 写入必须落 _VIRTUAL_PREFIX：validate_path 与 validate_command 都以此前缀为准。
- validate_command 只捕获**绝对路径**重定向；跳过：
    - 文件描述符重定向（如 2>&1）
    - 相对路径重定向
- 不解析 cd 后的相对路径（复杂，交给容器隔离兜底）。
- 不检查 tee（罕见，LLM 通常用 > 重定向）。
- 命中违规抛 SandboxPermissionError（LLM 可据错误重试、改路径）。
- 误拒优于放过：宁可拒绝合法路径，也不放过越界写入。
"""
from __future__ import annotations

import re

from poirot.backend.agents.sandbox.exceptions import SandboxPermissionError

_VIRTUAL_PREFIX = "/mnt/poirot/user-data/"
_REDIRECT_PATTERN = re.compile(r'>{1,2}\s*(/[^\s;|&]*)')


class DockerPathGuard:
    """Docker 路径白名单守卫：写入必须落挂载区。

    - validate_path(write=True)：路径必须以 /mnt/poirot/user-data/ 为前缀。
    - validate_command：bash 重定向目标（绝对路径）必须以同一前缀为准。
    - 读操作不限制（容器隔离兜底）。
    """

    def validate_path(self, path: str, *, write: bool = False) -> None:
        """写入路径校验：write=True 时要求路径落在 _VIRTUAL_PREFIX 下；否则放行。"""
        if not write:
            return
        if not path.startswith(_VIRTUAL_PREFIX):
            raise SandboxPermissionError(
                f"write path must be under {_VIRTUAL_PREFIX}: {path}",
                path=path,
                operation="validate",
            )

    def validate_command(self, command: str) -> None:
        """命令校验：所有绝对路径重定向目标必须落在 _VIRTUAL_PREFIX 下。"""
        for match in _REDIRECT_PATTERN.finditer(command):
            target = match.group(1)
            if not target.startswith(_VIRTUAL_PREFIX):
                raise SandboxPermissionError(
                    f"bash redirect target must be under {_VIRTUAL_PREFIX}: {target}",
                    path=target,
                    operation="validate_command",
                )