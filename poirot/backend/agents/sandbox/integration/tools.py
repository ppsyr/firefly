"""Sandbox 工具工厂 — 把 Sandbox 能力包装成 Agent 可调用的 @tool 工具集。

【整体职责】
基于给定的 SandboxProvider，工厂式地构造一组 LangChain @tool（bash / read_file /
write_file / list_dir / str_replace / present_files），供 Agent 在工具调用中使用。
工具内部通过 ContextVar 取当前 sandbox_id，再经 provider.get 拿到 Sandbox 实例。

【内容摘要】
- _BASH_OUTPUT_MAX_CHARS : bash 输出截断阈值（10000 字符）。
- WRITE_FILE_MAX_BYTES   : write_file 非 append 模式下的单次写入上限（5 MiB）。
- _truncate_output       : 长输出截断，超限追加 omitted 提示。
- _ensure_sandbox        : 从 ContextVar 取 sandbox_id + provider.get 拿 Sandbox；
                           缺失时抛 SandboxRuntimeError / SandboxNotFoundError。
- _make_bash_tool        : 构造 bash 工具（受 allow_host_bash 控制是否注册）。
- _make_read_file_tool   : 构造 read_file 工具。
- _make_write_file_tool  : 构造 write_file 工具（含大小上限 + append 语义）。
- _make_list_dir_tool    : 构造 list_dir 工具（树形输出）。
- _make_str_replace_tool : 构造 str_replace 工具（按 sandbox+path 加锁串行化）。
- _make_present_files_tool: 构造 present_files 工具（声明交付物，校验路径前缀）。
- make_sandbox_tools     : 工厂入口；闭包捕获 provider，产出工具列表。

【职责边界】
- 只负责：把 Sandbox 能力包装成 @tool、参数校验、输出截断/格式化、按需加锁。
- 不负责：Sandbox 的创建与生命周期（provider）、路径转换（translator）、
  安全校验（guard）、sandbox_id 的设置（middleware）。
- 闭包持有 provider 引用：工具通过 provider 间接访问 Sandbox，本身不持有 Sandbox 实例。

【INVARIANT】
- 所有工具通过 _ensure_sandbox 统一取 Sandbox，不直接 new / 不直接持有。
- bash 输出超 _BASH_OUTPUT_MAX_CHARS 自动截断。
- write_file 非 append 且内容超 WRITE_FILE_MAX_BYTES 时拒绝写入，提示改用 append 分块。
- str_replace 按 (sandbox.id, path) 加 file_operation_lock，保证同文件读改写串行。
- present_files 只接受 /mnt/poirot/user-data/ 前缀的路径，否则拒绝。
- allow_host_bash=False 时不注册 bash 工具（S2 安全加固）。
- 工具数量：注册 bash 时 6 个，否则 5 个。
"""
from __future__ import annotations

from langchain_core.tools import BaseTool, tool

from poirot.backend.agents.sandbox.contracts import SandboxProvider
from poirot.backend.agents.sandbox.exceptions import (
    SandboxNotFoundError,
    SandboxRuntimeError,
)
from poirot.backend.agents.sandbox.integration.context import get_sandbox_id
from poirot.backend.agents.sandbox.sandbox import Sandbox
from poirot.backend.agents.sandbox.utils.file_operation_lock import (
    get_file_operation_lock,
)

_BASH_OUTPUT_MAX_CHARS = 10000
WRITE_FILE_MAX_BYTES = 5 * 1024 * 1024


def _truncate_output(
    output: str, max_chars: int = _BASH_OUTPUT_MAX_CHARS
) -> str:
    """截断长输出；超限时保留前 max_chars 字符并追加 omitted 提示。"""
    if len(output) <= max_chars:
        return output
    return (
        output[:max_chars]
        + f"\n... (truncated, {len(output) - max_chars} chars omitted)"
    )


def _ensure_sandbox(provider: SandboxProvider) -> Sandbox:
    """从 ContextVar 取 sandbox_id，再经 provider.get 获取 Sandbox。

    未绑定 sandbox_id 时抛 SandboxRuntimeError；
    provider 中查无此 sandbox 时抛 SandboxNotFoundError。
    """
    sandbox_id = get_sandbox_id()
    if sandbox_id is None:
        raise SandboxRuntimeError(
            "no sandbox in context (Stage 4 middleware not set)"
        )
    sandbox = provider.get(sandbox_id)
    if sandbox is None:
        raise SandboxNotFoundError(sandbox_id)
    return sandbox


def _make_bash_tool(provider: SandboxProvider) -> BaseTool:
    @tool("bash", parse_docstring=True)
    def bash_tool(command: str) -> str:
        """Execute a bash command in the sandbox.

        Args:
            command: The bash command to execute.
        """
        output = _ensure_sandbox(provider).execute_command(command)
        return _truncate_output(output)

    return bash_tool


def _make_read_file_tool(provider: SandboxProvider) -> BaseTool:
    @tool("read_file", parse_docstring=True)
    def read_file_tool(path: str) -> str:
        """Read the contents of a file.

        Args:
            path: Virtual path to the file (e.g. /mnt/poirot/user-data/workspace/file.txt).
        """
        return _ensure_sandbox(provider).read_file(path)

    return read_file_tool


def _make_write_file_tool(provider: SandboxProvider) -> BaseTool:
    @tool("write_file", parse_docstring=True)
    def write_file_tool(
        path: str, content: str, append: bool = False
    ) -> str:
        """Write content to a file. Creates parent directories if needed.

        Args:
            path: Virtual path to the file.
            content: Text content to write.
            append: If True, append to file; if False, overwrite.
        """
        if not append and len(content.encode("utf-8")) > WRITE_FILE_MAX_BYTES:
            raise SandboxRuntimeError(
                f"write_file content exceeds {WRITE_FILE_MAX_BYTES} bytes limit; "
                "use append=True to write in chunks"
            )
        _ensure_sandbox(provider).write_file(path, content, append=append)
        return f"wrote {len(content)} chars to {path}"

    return write_file_tool


def _make_list_dir_tool(provider: SandboxProvider) -> BaseTool:
    @tool("list_dir", parse_docstring=True)
    def list_dir_tool(path: str, max_depth: int = 2) -> str:
        """List directory contents in tree format.

        Args:
            path: Virtual path to the directory.
            max_depth: Maximum depth to traverse (default 2).
        """
        entries = _ensure_sandbox(provider).list_dir(path, max_depth=max_depth)
        if not entries:
            return "(empty)"
        lines: list[str] = []
        for entry in entries:
            parts = entry.replace("\\", "/").split("/")
            indent = "  " * (len(parts) - 1)
            lines.append(f"{indent}{parts[-1]}")
        return "\n".join(lines)

    return list_dir_tool


def _make_str_replace_tool(provider: SandboxProvider) -> BaseTool:
    @tool("str_replace", parse_docstring=True)
    def str_replace_tool(
        path: str, old_str: str, new_str: str, replace_all: bool = False
    ) -> str:
        """Replace text in a file. Serialized per (sandbox, path).

        Args:
            path: Virtual path to the file.
            old_str: Text to find.
            new_str: Replacement text.
            replace_all: If True, replace all occurrences; if False, replace first.
        """
        sandbox = _ensure_sandbox(provider)
        lock = get_file_operation_lock(sandbox.id, path)
        with lock:
            content = sandbox.read_file(path)
            if old_str not in content:
                raise SandboxRuntimeError(f"old_str not found in {path}")
            count = content.count(old_str)
            if replace_all:
                new_content = content.replace(old_str, new_str)
            else:
                new_content = content.replace(old_str, new_str, 1)
            sandbox.write_file(path, new_content)
        return f"replaced {count if replace_all else 1} occurrence(s) in {path}"

    return str_replace_tool


def make_sandbox_tools(provider: SandboxProvider, allow_host_bash: bool = True) -> list[BaseTool]:
    """工厂入口：构造 sandbox 工具集，闭包捕获 provider。

    返回 6 个 @tool：bash / read_file / write_file / list_dir / str_replace / present_files。
    工具内部用 ContextVar get_sandbox_id 取 sandbox_id，再经 provider.get 拿 Sandbox。
    allow_host_bash=False 时不注册 bash 工具（S2 安全加固）。
    """
    tools = []
    if allow_host_bash:
        tools.append(_make_bash_tool(provider))
    tools.extend([
        _make_read_file_tool(provider),
        _make_write_file_tool(provider),
        _make_list_dir_tool(provider),
        _make_str_replace_tool(provider),
        _make_present_files_tool(provider),
    ])
    return tools


def _make_present_files_tool(provider: SandboxProvider) -> BaseTool:
    @tool("present_files", parse_docstring=True)
    def present_files_tool(paths: list[str]) -> str:
        """Declare files as deliverable artifacts. The system generates downloadable URLs for the user to access these files via browser.

        Call this after generating final output files (pptx, csv, py, pdf, etc.) in the sandbox.
        Files MUST be under /mnt/poirot/user-data/ (e.g. /mnt/poirot/user-data/workspace/report.pptx).
        Files in other paths (e.g. /home/...) are not accessible to the host and cannot be delivered.

        Args:
            paths: Virtual paths to files under /mnt/poirot/user-data/ (e.g. /mnt/poirot/user-data/workspace/report.pptx).
        """
        _REQUIRED_PREFIX = "/mnt/poirot/user-data/"
        invalid = [p for p in paths if not p.startswith(_REQUIRED_PREFIX)]
        if invalid:
            raise SandboxRuntimeError(
                f"present_files: paths must be under {_REQUIRED_PREFIX}. "
                f"Invalid: {invalid}. Copy files there first with: "
                f"bash('cp <src> /mnt/poirot/user-data/workspace/<filename>')"
            )
        return f"Presented {len(paths)} file(s): {', '.join(paths)}"

    return present_files_tool