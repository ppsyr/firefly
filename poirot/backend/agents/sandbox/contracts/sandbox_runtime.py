"""SandboxRuntime — 中立沙箱运行时协议（裸执行契约）。

【整体职责】
定义沙箱「裸执行」的最小接口：命令执行与文件读写。方案 C 三组件之一，
只负责执行，不涉及路径翻译、不做安全检查，由 Sandbox 编排层负责调用前后处理。
Local / Docker / E2B 各写 adapter；异构差异（如 E2B 无 glob）封装在 adapter 内。

【内容摘要】
- SandboxRuntime          : Protocol（runtime_checkable），裸执行契约。
- exec_command            : 执行命令，返回输出。
- read_file               : 读文件。
- write_file              : 写文件（支持 append）。
- list_dir                : 列目录（带深度与条目上限）。
- glob                    : 按 pattern 匹配路径，返回 (结果, truncated)。
- grep                    : 内容检索，返回 (GrepMatch 列表, truncated)。
- download_file           : 下载文件字节流。
- update_file             : 上传/覆盖文件字节流。
- close                   : 释放运行时资源。

【职责边界】
- 只负责：裸执行（exec / read / write / list / glob / grep / download / update / close）。
- 不负责：路径翻译（Translator）、安全检查（Guard）、编排顺序（Sandbox）、
  生命周期缓存（Provider）、基础设施 CRUD（Backend）。
- 不持有状态：Protocol 只定义接口；状态由具体 adapter 持有。

【INVARIANT】
- 路径契约由调用方保证：Local 路径已翻译为物理路径；Docker/E2B 直传虚拟路径。
- 异常类型统一：全抛 SandboxError 子类
  （SandboxCommandError / SandboxFileError / SandboxPermissionError /
    SandboxFileNotFoundError / SandboxRuntimeError）。
- runtime 实现负责包装内置异常：
  subprocess.CalledProcessError → SandboxCommandError，
  FileNotFoundError → SandboxFileNotFoundError 等。
- 工具层只需 catch SandboxError，由 ToolCallMiddleware 统一转 error ToolMessage。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from poirot.backend.agents.sandbox.types import GrepMatch


@runtime_checkable
class SandboxRuntime(Protocol):
    """中立沙箱运行时协议（裸执行契约）。

    方案 C 三组件之一。只负责裸执行（exec/read/write），不知路径翻译、不做安全检查。
    Local / Docker / E2B 各写 adapter。异构差异（E2B 无 glob）封装在 adapter 内。

    所有方法遵守调用方传入的路径契约（Local 路径已翻译为物理路径；Docker/E2B 直传虚拟路径）。
    异常类型：全抛 SandboxError 子类（SandboxCommandError / SandboxFileError /
    SandboxPermissionError / SandboxFileNotFoundError / SandboxRuntimeError）。
    runtime 实现负责包装内置异常（subprocess.CalledProcessError → SandboxCommandError，
    FileNotFoundError → SandboxFileNotFoundError 等）。
    工具层只需 catch SandboxError，由 ToolCallMiddleware 统一转 error ToolMessage。
    """

    def exec_command(self, command: str) -> str: ...

    def read_file(self, path: str) -> str: ...

    def write_file(self, path: str, content: str, append: bool = False) -> None: ...

    def list_dir(self, path: str, max_depth: int = 2, max_entries: int = 1000) -> list[str]: ...

    def glob(
        self,
        path: str,
        pattern: str,
        *,
        include_dirs: bool = False,
        max_results: int = 200,
    ) -> tuple[list[str], bool]: ...

    def grep(
        self,
        path: str,
        pattern: str,
        *,
        glob: str | None = None,
        literal: bool = False,
        case_sensitive: bool = False,
        max_results: int = 100,
    ) -> tuple[list[GrepMatch], bool]: ...

    def download_file(self, path: str) -> bytes: ...

    def update_file(self, path: str, content: bytes) -> None: ...

    def close(self) -> None: ...