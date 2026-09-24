"""CodexRuntime — ACP 协议实现（启动 codex-acp 子进程 + stdio JSON-RPC）。

【整体职责】
实现 SpecialistRuntime 契约：通过 ACP 协议（agent-client-protocol）启动 codex-acp
子进程，建立 stdio JSON-RPC 会话，把 goal 作为 prompt 发给子进程，收集流式返回的
文本块，拼成原始输出返回。

【内容摘要】
- CodexRuntime           : runtime 主类，实现 invoke()。
- invoke()               : 同步入口，用 asyncio.run 包装异步 _run_acp_session。
- _run_acp_session()     : 异步 ACP 会话：spawn 子进程 → initialize → new_session → prompt → 收 chunks。
- _build_mcp_config()    : 构造 ACP new_session 的 MCP servers 配置（含 SpecialistMcpServer）。
- _build_env()           : 透传 auth 相关环境变量给 codex-acp 子进程（白名单）。

【职责边界】
- 只负责：启动 ACP 子进程、建立会话、发 prompt、收集输出、异常归一化。
- 不负责：上下文摘要（ContextSummarizer 负责）、结果摘要（ResultSummarizer 负责）、
  凭证发现（CredentialProvider 负责）、沙箱生命周期（sandbox 层负责）。
- 不持有运行时状态：每次 invoke 启动新进程 + 完成关闭，不做连接池。

【INVARIANT】
- sync only：对外只暴露同步 invoke，内部用 asyncio.run 包装异步会话。
- 每次 invoke 启动新进程，完成即关闭，不做 pool。
- acp 包 lazy import：未安装时抛 SpecialistStartupError（提示安装命令）。
- specialist 黑盒：codex-acp 自带 model + 自管 ReAct loop，Poirot 不介入内部。
- 超时 kill：asyncio.TimeoutError → SpecialistTimeoutError。
- 命令缺失：FileNotFoundError → SpecialistStartupError。
- 其他异常：统一转 SpecialistCrashError；但 SpecialistError 子类原样抛出。
- env 透传走白名单：仅 CODEX_AUTH_PATH / OPENAI_API_KEY / CODEX_HOME；
  不透传全部 env，防敏感信息泄漏；无命中时返回 None（子进程继承父 env）。
- MCP 配置仅在 sandbox_id 非 None 时构造；sandbox_id 为 None 时返回空列表。
- chunks 收集走自定义 Client 子类；非 TextContentBlock 内容忽略。
- prompt 超时取 request.timeout_seconds。
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from poirot.backend.agents.multiagent.exceptions import (
    SpecialistCrashError,
    SpecialistError,
    SpecialistStartupError,
    SpecialistTimeoutError,
)
from poirot.backend.agents.multiagent.runtimes import append_sandbox_url_args
from poirot.backend.agents.multiagent.types import (
    SpecialistRawResult,
    SpecialistRequest,
)


class CodexRuntime:
    """ACP 协议 runtime（codex-acp 子进程 + stdio JSON-RPC）。

    sync only；每次 invoke 启动新进程 + 完成关闭。
    """

    def __init__(
        self,
        command: str = "npx",
        args: tuple[str, ...] = ("-y", "@zed-industries/codex-acp"),
        sandbox_provider=None,
    ) -> None:
        """初始化。

        Args:
            command: 启动命令，默认 "npx"。
            args: 命令参数，默认拉起 @zed-industries/codex-acp。
            sandbox_provider: 沙箱 provider，用于构造 MCP 配置里的 sandbox URL 参数。
        """
        self._command = command
        self._args = args
        self._sandbox_provider = sandbox_provider

    def invoke(self, request: SpecialistRequest) -> SpecialistRawResult:
        """执行 specialist 调用：启动 codex-acp 会话，返回原始输出。

        异常映射：
        - asyncio.TimeoutError → SpecialistTimeoutError。
        - FileNotFoundError → SpecialistStartupError（命令不存在）。
        - SpecialistError 子类 → 原样抛出（不包装）。
        - 其他异常 → SpecialistCrashError。

        Args:
            request: specialist 调用请求（含 goal / timeout_seconds / sandbox_id）。

        Returns:
            含 raw_output 与 duration_seconds 的 SpecialistRawResult。
        """
        start = time.time()
        try:
            raw_output = asyncio.run(self._run_acp_session(request))
        except asyncio.TimeoutError:
            raise SpecialistTimeoutError(
                timeout_seconds=request.timeout_seconds,
            )
        except SpecialistError:
            raise
        except FileNotFoundError:
            raise SpecialistStartupError(
                f"codex-acp command not found: {self._command} {' '.join(self._args)}"
            )
        except Exception as e:
            raise SpecialistCrashError(str(e))

        return SpecialistRawResult(
            raw_output=raw_output,
            duration_seconds=time.time() - start,
        )

    async def _run_acp_session(self, request: SpecialistRequest) -> str:
        """异步 ACP 会话：spawn 子进程 → initialize → new_session → prompt → 收集输出。

        流程：
        1. lazy import acp 包；未装 → SpecialistStartupError。
        2. _build_mcp_config 构造 MCP servers 配置。
        3. 定义 _Collector（Client 子类）收集 TextContentBlock 文本。
        4. spawn_agent_process 启动子进程 + 建立连接。
        5. initialize → new_session → prompt（带 timeout）。
        6. 退出 async with 时自动关闭子进程。
        7. 拼接 chunks 返回。
        """
        try:
            from acp import PROTOCOL_VERSION, Client, text_block
            from acp.schema import (
                ClientCapabilities,
                Implementation,
                TextContentBlock,
            )
            from acp import spawn_agent_process
        except ImportError as e:
            raise SpecialistStartupError(
                f"acp package not installed: {e}. Run: pip install agent-client-protocol"
            )

        mcp_servers = self._build_mcp_config(request.sandbox_id)
        chunks: list[str] = []

        class _Collector(Client):
            """收集会话更新中的文本块。"""

            async def session_update(self, session_id: str, update, **kwargs) -> None:
                try:
                    if hasattr(update, "content") and isinstance(
                        update.content, TextContentBlock
                    ):
                        chunks.append(update.content.text)
                except Exception:
                    pass

        client = _Collector()
        agent_env = self._build_env()

        async with spawn_agent_process(
            client, self._command, *self._args, env=agent_env,
        ) as (conn, proc):
            await conn.initialize(
                protocol_version=PROTOCOL_VERSION,
                client_capabilities=ClientCapabilities(),
                client_info=Implementation(
                    name="poirot", title="Poirot", version="0.1.0",
                ),
            )
            session_kwargs: dict[str, Any] = {
                "cwd": request.artifacts_path or ".",
                "mcp_servers": mcp_servers,
            }
            session = await conn.new_session(**session_kwargs)
            await asyncio.wait_for(
                conn.prompt(
                    session_id=session.session_id,
                    prompt=[text_block(request.goal)],
                ),
                timeout=request.timeout_seconds,
            )

        return "".join(chunks)

    def _build_mcp_config(self, sandbox_id: str | None) -> list[dict[str, Any]]:
        """构造 ACP new_session 的 MCP servers 配置（含 SpecialistMcpServer）。

        - sandbox_id 为 None → 返回空列表（不挂载 MCP）。
        - 非 None → 构造 stdio 类型的 MCP server 配置，命令为 python -m
          ...specialist_mcp_server --sandbox-id <id>（附加 sandbox URL 参数）。
        """
        if sandbox_id is None:
            return []
        args = [
            "-m",
            "poirot.backend.agents.multiagent.mcp.specialist_mcp_server",
            "--sandbox-id",
            sandbox_id,
        ]
        append_sandbox_url_args(args, self._sandbox_provider, sandbox_id)
        return [
            {
                "name": "poirot_sandbox",
                "type": "stdio",
                "command": "python",
                "args": args,
            }
        ]

    def _build_env(self) -> dict[str, str] | None:
        """透传 auth 相关环境变量给 codex-acp 子进程（白名单）。

        透传变量：
        - CODEX_AUTH_PATH：codex CLI 凭证文件路径（默认 ~/.codex/auth.json）。
        - OPENAI_API_KEY：API key 模式（非 OAuth 订阅）。
        - CODEX_HOME：codex CLI 自定义配置目录。

        - 无命中 → 返回 None（子进程继承父 env）。
        - 只透传白名单，不透传全部 env，防敏感信息泄漏。
        """
        env: dict[str, str] = {}
        # CODEX_AUTH_PATH：codex CLI 凭证文件路径（~/.codex/auth.json 默认）
        auth_path = os.getenv("CODEX_AUTH_PATH")
        if auth_path:
            env["CODEX_AUTH_PATH"] = auth_path
        # OPENAI_API_KEY：API key 模式（非 OAuth 订阅）
        openai_key = os.getenv("OPENAI_API_KEY")
        if openai_key:
            env["OPENAI_API_KEY"] = openai_key
        # CODEX_HOME：codex CLI 自定义配置目录（用户自定义 CODEX_HOME 覆盖默认 ~/.codex）
        codex_home = os.getenv("CODEX_HOME")
        if codex_home:
            env["CODEX_HOME"] = codex_home
        return env or None