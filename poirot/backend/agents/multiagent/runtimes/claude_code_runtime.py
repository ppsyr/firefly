"""ClaudeCodeRuntime — CLI 直接调实现（claude --print 子进程 + stdout 解析）。

【整体职责】
实现 SpecialistRuntime 契约：每次 invoke 启动一个 claude CLI 子进程，
通过 `claude --print <goal>` 执行任务，从 stdout 取原始输出，组装成 SpecialistRawResult。

【内容摘要】
- ClaudeCodeRuntime          : runtime 主类，实现 invoke()。
- invoke()                   : 构建命令 + env → subprocess.run → 解析输出 / 归一化异常。
- _build_command()           : 构造 `claude --print <goal>` 命令。
- _build_env()               : 透传 auth 相关环境变量给子进程。
- _build_mcp_add_command()   : 构造 `claude mcp add` 命令注册 SpecialistMcpServer。
- configure_mcp()            : 执行 mcp add，注册 Poirot MCP server。

【职责边界】
- 只负责：构造命令、构造 env、启动子进程、解析 stdout、异常归一化。
- 不负责：上下文摘要（ContextSummarizer 负责）、结果摘要（ResultSummarizer 负责）、
  凭证发现（CredentialProvider 负责）、沙箱生命周期（sandbox 层负责）。
- 不持有运行时状态：每次 invoke 启动新进程，不跨调用复用。

【INVARIANT】
- sync only：只实现同步 invoke，不提供异步版本。
- 不走 ACP：直接调 CLI，不依赖 Claude Code ACP adapter。
- specialist 黑盒：claude CLI 自带 model + 自管 ReAct loop，Poirot 不介入其内部。
- 每次 invoke 启动新子进程，不复用进程。
- 超时 kill：subprocess.TimeoutExpired → SpecialistTimeoutError。
- 命令缺失：FileNotFoundError → SpecialistStartupError。
- 非零退出：returncode != 0 → SpecialistCrashError（含 exit_code）。
- env 透传：仅透传 auth 相关变量（CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_AUTH_TOKEN /
  ANTHROPIC_API_KEY / CLAUDE_CODE_CREDENTIALS_PATH）；无 auth 变量时返回 None，
  让子进程继承父 env。
- 有 auth 变量时 merge 父 env + auth，保证 PATH / HOME 等基础 env 可用。
- 超时时间取 request.timeout_seconds。
"""
from __future__ import annotations

import os
import subprocess
import time

from poirot.backend.agents.multiagent.exceptions import (
    SpecialistCrashError,
    SpecialistStartupError,
    SpecialistTimeoutError,
)
from poirot.backend.agents.multiagent.runtimes import append_sandbox_url_args
from poirot.backend.agents.multiagent.types import (
    SpecialistRawResult,
    SpecialistRequest,
)


class ClaudeCodeRuntime:
    """CLI 直接调 runtime（`claude --print` + stdout 解析）。

    sync only；每次 invoke 启动新进程。
    """

    def __init__(
        self, command: str = "claude", sandbox_provider=None
    ) -> None:
        """初始化。

        Args:
            command: claude CLI 命令名或路径，默认 "claude"。
            sandbox_provider: 沙箱 provider，用于构造 sandbox URL 参数。
        """
        self._command = command
        self._sandbox_provider = sandbox_provider

    def invoke(self, request: SpecialistRequest) -> SpecialistRawResult:
        """执行 specialist 调用：启动 claude CLI 子进程，返回原始输出。

        - subprocess.TimeoutExpired → SpecialistTimeoutError。
        - FileNotFoundError → SpecialistStartupError（命令不存在）。
        - returncode != 0 → SpecialistCrashError。

        Args:
            request: specialist 调用请求（含 goal / timeout_seconds）。

        Returns:
            含 stdout 与 duration_seconds 的 SpecialistRawResult。
        """
        start = time.time()
        cmd = self._build_command(request)
        env = self._build_env()

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=request.timeout_seconds,
                env=env,
            )
        except subprocess.TimeoutExpired:
            raise SpecialistTimeoutError(
                timeout_seconds=request.timeout_seconds,
            )
        except FileNotFoundError:
            raise SpecialistStartupError(
                f"claude command not found: {self._command}"
            )

        if result.returncode != 0:
            raise SpecialistCrashError(
                f"claude exited with code {result.returncode}",
                exit_code=result.returncode,
            )

        return SpecialistRawResult(
            raw_output=result.stdout,
            duration_seconds=time.time() - start,
        )

    def _build_command(self, request: SpecialistRequest) -> list[str]:
        """构造 `claude --print <goal>` 命令，goal 作为位置参数传入。"""
        return [self._command, "--print", request.goal]

    def _build_env(self) -> dict[str, str] | None:
        """透传 auth 相关环境变量给 claude 子进程。

        透传变量：CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_AUTH_TOKEN /
        ANTHROPIC_API_KEY / CLAUDE_CODE_CREDENTIALS_PATH。

        - 无 auth 变量 → 返回 None（子进程继承父 env）。
        - 有 auth 变量 → 返回 merge 后的 env（父 env + auth 覆盖），
          保证 PATH / HOME 等基础 env 可用。
        """
        auth_env: dict[str, str] = {}
        for var in (
            "CLAUDE_CODE_OAUTH_TOKEN",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_API_KEY",
            "CLAUDE_CODE_CREDENTIALS_PATH",
        ):
            val = os.getenv(var)
            if val:
                auth_env[var] = val
        if not auth_env:
            return None
        # merge：父进程 env + auth vars 覆盖（保证 PATH / HOME 等基础 env 可用）
        return {**os.environ, **auth_env}

    def _build_mcp_add_command(self, sandbox_id: str) -> list[str]:
        """构造 `claude mcp add` 命令，注册 SpecialistMcpServer。

        命令形式：
            claude mcp add poirot_sandbox -- python -m
            poirot.backend.agents.multiagent.mcp.specialist_mcp_server
            --sandbox-id <sandbox_id> [+ sandbox URL 参数]
        """
        cmd = [
            self._command,
            "mcp",
            "add",
            "poirot_sandbox",
            "--",
            "python",
            "-m",
            "poirot.backend.agents.multiagent.mcp.specialist_mcp_server",
            "--sandbox-id",
            sandbox_id,
        ]
        append_sandbox_url_args(cmd, self._sandbox_provider, sandbox_id)
        return cmd

    def configure_mcp(self, sandbox_id: str) -> None:
        """执行 `claude mcp add`，把 SpecialistMcpServer 注册到 claude CLI。

        由 specialist 在 invoke 前调用（或由 bootstrap 调用）。
        超时 30 秒，输出丢弃。
        """
        cmd = self._build_mcp_add_command(sandbox_id)
        subprocess.run(cmd, capture_output=True, timeout=30)