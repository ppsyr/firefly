"""PiRuntime — RPC mode 协议实现（pi --mode rpc 子进程 + stdio JSON-RPC）。

【整体职责】
实现 SpecialistRuntime 契约：每次 invoke 启动一个 `pi --mode rpc` 子进程，
通过 stdin 发 prompt、从 stdout 读 JSON 事件流，收集文本增量与 token 用量，
返回 SpecialistRawResult。强制走 Poirot SpecialistMcpServer（禁用 pi 自带工具）。

【内容摘要】
- PiRuntimeConfig             : runtime 配置（命令 / mode 参数 / provider / model / thinking）。
- PiRuntime                   : runtime 主类，实现 invoke()。
- invoke()                    : 同步入口：起进程 → 发 prompt → 收事件 → 归一化异常。
- _run_rpc_session()          : 启动子进程 + 读写 stdio 的 JSON-RPC 会话。
- _build_command()            : 组装 pi CLI 命令（含 --no-builtin-tools + -e extension）。
- _build_env()                : 透传凭证 env（国内 provider 优先）+ MCP endpoint。
- _build_prompt()             : 构造 prompt（goal + context + criteria + 三段输出格式）。
- _extract_usage()            : 从 agent_end 事件提取 TokenUsage。
- _resolve_mcp_endpoint()     : 解析 SpecialistMcpServer 的 stdio 启动命令。
- _poirot_extension_path()    : 返回 Poirot sandbox bridge extension 的路径。

【职责边界】
- 只负责：启动 pi 子进程、发 prompt、收事件、抽输出与 usage、异常归一化。
- 不负责：上下文摘要（ContextSummarizer 负责）、结果摘要（ResultSummarizer 负责）、
  凭证发现（CredentialProvider 负责）、沙箱生命周期（sandbox 层负责）。
- 不持有运行时状态：每次 invoke 启动新进程 + 完成关闭，不做 pool。

【INVARIANT】
- sync only：对外只暴露同步 invoke。
- 每次 invoke 启动新进程，完成即关闭，不做 pool。
- pi 黑盒：pi CLI 自带 model + 自管 ReAct loop，Poirot 不介入内部。
- 强制走 SpecialistMcpServer：命令必带 `--no-builtin-tools` 与
  `-e <pi-sandbox-bridge/index.ts>`，禁用 pi 自带工具。
- 不传 --system-prompt：MVP 不定制 system prompt，靠 _build_prompt 在 user message
  塞 context + 三段输出格式要求。
- env 透传走白名单：凭证 env（国内 provider 靠前）+ PI_CODING_AGENT_DIR +
  POIROT_SANDBOX_MCP_ENDPOINT（sandbox_id 非 None 时）。
- 无命中 env → 返回 None（子进程继承父 env）；有命中 → {**os.environ, **auth_env}。
- 异常映射：TimeoutExpired → SpecialistTimeoutError；FileNotFoundError →
  SpecialistStartupError；SpecialistError 子类原样抛出；其他 → SpecialistCrashError。
- 事件处理：message_update(text_delta) 累积输出；agent_end 提取 usage 并结束；
  extension_error 转 SpecialistCrashError。
- 进程清理：finally 里关 stdin、terminate、wait(5s)，超时则 kill。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
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
    TokenUsage,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PiRuntimeConfig:
    """Pi runtime 配置。

    provider / model / thinking_level 留空时用 pi 默认（pi 自己选）。
    """

    command: str = "pi"
    mode_args: tuple[str, ...] = ("--mode", "rpc", "--no-session")
    provider: str | None = None
    model: str | None = None
    thinking_level: str = "medium"
    extra_args: tuple[str, ...] = ()


class PiRuntime:
    """Pi coding agent runtime（RPC mode，stdio JSON-RPC）。

    sync only；每次 invoke 启动新 pi 子进程 + 完成关闭。
    强制走 Poirot SpecialistMcpServer：--no-builtin-tools 禁用 pi 自带
    read/write/edit/bash，-e poirot-sandbox-bridge 加载 Poirot sandbox bridge
    extension，所有操作走 Poirot MCP 接口。
    """

    def __init__(self, config: PiRuntimeConfig | None = None, sandbox_provider=None) -> None:
        """初始化。

        Args:
            config: PiRuntimeConfig；为 None 时用默认配置。
            sandbox_provider: 沙箱 provider，用于解析 MCP endpoint 的 sandbox URL 参数。
        """
        self._config = config or PiRuntimeConfig()
        self._sandbox_provider = sandbox_provider

    def invoke(self, request: SpecialistRequest) -> SpecialistRawResult:
        """执行 specialist 调用：启动 pi RPC 会话，返回原始输出 + token 用量。

        异常映射：
        - subprocess.TimeoutExpired → SpecialistTimeoutError。
        - FileNotFoundError → SpecialistStartupError（命令不存在）。
        - SpecialistError 子类 → 原样抛出（不包装）。
        - 其他异常 → SpecialistCrashError。

        Args:
            request: specialist 调用请求（含 goal / timeout_seconds / sandbox_id）。

        Returns:
            含 raw_output / usage / duration_seconds 的 SpecialistRawResult。
        """
        start = time.time()
        try:
            raw_output, usage = self._run_rpc_session(request)
        except subprocess.TimeoutExpired:
            raise SpecialistTimeoutError(
                timeout_seconds=request.timeout_seconds,
            )
        except FileNotFoundError:
            raise SpecialistStartupError(
                f"pi command not found: {self._config.command}. "
                "Install: npm install -g @earendil-works/pi-coding-agent"
            )
        except SpecialistError:
            raise
        except Exception as e:
            raise SpecialistCrashError(str(e))

        return SpecialistRawResult(
            raw_output=raw_output,
            usage=usage,
            duration_seconds=time.time() - start,
        )

    def _run_rpc_session(
        self, request: SpecialistRequest
    ) -> tuple[str, TokenUsage | None]:
        """启动 pi --mode rpc 子进程，发 prompt，收 events，返回 final text + usage。

        流程：
        1. _build_command + _build_env。
        2. subprocess.Popen 启动子进程。
        3. 写一行 JSON prompt 到 stdin。
        4. 逐行读 stdout，解析 JSON 事件：
           - message_update(text_delta)：累积文本。
           - agent_end：从最后一条 assistant message 抽 usage，结束循环。
           - extension_error：转 SpecialistCrashError。
        5. finally：关 stdin、terminate、wait(5s)，超时则 kill。
        """
        cmd = self._build_command(request)
        env = self._build_env(request)

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            prompt = self._build_prompt(request)
            cmd_json = json.dumps(
                {"type": "prompt", "message": prompt, "id": "req-1"}
            )
            proc.stdin.write(cmd_json + "\n")
            proc.stdin.flush()

            chunks: list[str] = []
            usage_data: dict[str, Any] = {}
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type")
                if etype == "message_update":
                    ame = event.get("assistantMessageEvent", {})
                    if ame.get("type") == "text_delta":
                        chunks.append(ame["delta"])

                elif etype == "agent_end":
                    # 从最后一条 assistant message 提取 usage
                    msgs = event.get("messages", [])
                    for msg in reversed(msgs):
                        if msg.get("role") == "assistant":
                            usage_data = msg.get("usage", {}) or {}
                            break
                    break

                elif etype == "extension_error":
                    raise SpecialistCrashError(
                        f"pi extension error: {event.get('error', 'unknown')}"
                    )

            raw_output = "".join(chunks)
            usage = self._extract_usage(usage_data)
            return raw_output, usage

        finally:
            proc.stdin.close()
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _build_command(self, request: SpecialistRequest) -> list[str]:
        """组装 pi CLI 命令。

        包含：mode 参数、provider / model / thinking（若配置）、
        `--no-builtin-tools`、`-e <extension 路径>`、extra_args。
        不传 --system-prompt（MVP 靠 _build_prompt 在 user message 塞 context）。
        """
        cmd = [self._config.command, *self._config.mode_args]
        if self._config.provider:
            cmd.extend(["--provider", self._config.provider])
        if self._config.model:
            cmd.extend(["--model", self._config.model])
        if self._config.thinking_level:
            cmd.extend(["--thinking", self._config.thinking_level])
        # 禁用 pi 自带工具，加载 Poirot sandbox bridge extension
        cmd.extend(["--no-builtin-tools"])
        cmd.extend(["-e", str(self._poirot_extension_path())])
        # 不传 --system-prompt（MVP 靠 _build_prompt 在 user message 塞 context）
        cmd.extend(self._config.extra_args)
        return cmd

    def _build_env(self, request: SpecialistRequest) -> dict[str, str] | None:
        """透传凭证 env vars + Poirot sandbox MCP endpoint。

        - 凭证 env：国内 provider 优先（DeepSeek/Kimi/MiniMax/Xiaomi 靠前），
          国外大厂与聚合服务在后。
        - PI_CODING_AGENT_DIR：自定义配置目录（可选）。
        - POIROT_SANDBOX_MCP_ENDPOINT：sandbox_id 非 None 时传入，
          供 pi extension 连接 Poirot SpecialistMcpServer。
        - 无命中 → 返回 None（子进程继承父 env）。
        - 有命中 → {**os.environ, **auth_env}，保证 PATH/HOME 等基础 env 可用。
        """
        auth_env: dict[str, str] = {}
        # 国内 provider 优先（便宜优先）
        pi_env_vars = [
            "DEEPSEEK_API_KEY",
            "KIMI_API_KEY",
            "MINIMAX_API_KEY",
            "XIAOMI_API_KEY",
            "ZAI_API_KEY",
            # 国外大厂
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GEMINI_API_KEY",
            # 聚合/其他
            "OPENROUTER_API_KEY",
            "GROQ_API_KEY",
            "XAI_API_KEY",
            "MISTRAL_API_KEY",
            "TOGETHER_API_KEY",
        ]
        for var in pi_env_vars:
            val = os.getenv(var)
            if val:
                auth_env[var] = val

        # PI_CODING_AGENT_DIR（自定义配置目录）
        pi_dir = os.getenv("PI_CODING_AGENT_DIR")
        if pi_dir:
            auth_env["PI_CODING_AGENT_DIR"] = pi_dir

        # 传 Poirot SpecialistMcpServer endpoint（sandbox_id 绑定）
        if request.sandbox_id:
            auth_env["POIROT_SANDBOX_MCP_ENDPOINT"] = self._resolve_mcp_endpoint(
                request.sandbox_id
            )

        if not auth_env:
            return None
        # merge：父进程 env + auth vars 覆盖（保证 PATH/HOME 等基础 env 可用）
        return {**os.environ, **auth_env}

    def _build_prompt(self, request: SpecialistRequest) -> str:
        """构造给 pi 的 prompt。

        pi 用自己的默认 system prompt（通用 coding agent）。
        Poirot 在 user message 里塞 goal + context_summary + success_criteria +
        三段输出格式要求（What You Did / Success / Gaps）。
        """
        parts = [request.goal]
        if request.context_summary:
            parts.append(f"\n\n## Context\n{request.context_summary}")
        if request.success_criteria:
            parts.append(f"\n\n## Success Criteria\n{request.success_criteria}")
        parts.append(
            "\n\n## Output Format\n"
            "After completing the task, summarize in three sections:\n"
            "## What You Did\n"
            "- Files changed (paths + brief description)\n"
            "- Commands run\n"
            "## Success\n"
            "- Whether success criteria are met (yes/no/partial)\n"
            "- Evidence (test results, file existence, etc.)\n"
            "## Gaps\n"
            "- Any incomplete parts or blockers\n"
            "- Next steps if incomplete"
        )
        return "".join(parts)

    def _extract_usage(
        self, usage_data: dict[str, Any]
    ) -> TokenUsage | None:
        """从 pi agent_end 事件的 usage 字段提取 TokenUsage。

        - usage_data 为空 → None。
        - tokens 缺失或类型错误 → None。
        """
        if not usage_data:
            return None
        try:
            tokens = usage_data.get("tokens") or {}
            return TokenUsage(
                prompt_tokens=int(tokens.get("input", 0)),
                completion_tokens=int(tokens.get("output", 0)),
                total_tokens=int(tokens.get("total", 0)),
            )
        except (TypeError, ValueError):
            return None

    def _resolve_mcp_endpoint(self, sandbox_id: str) -> str:
        """解析 Poirot SpecialistMcpServer endpoint（sandbox_id 绑定）。

        MVP：返回 SpecialistMcpServer 的 stdio 启动命令字符串
        （pi extension 通过此命令连接）。
        若 sandbox_provider 有 SandboxInfo，追加 --sandbox-url + --sandbox-root。
        """
        parts = [
            "python",
            "-m",
            "poirot.backend.agents.multiagent.mcp.specialist_mcp_server",
            "--sandbox-id",
            sandbox_id,
        ]
        append_sandbox_url_args(parts, self._sandbox_provider, sandbox_id)
        return " ".join(parts)

    def _poirot_extension_path(self) -> str:
        """返回 Poirot sandbox bridge extension 的路径。

        extension 注册 8 个工具转发到 Poirot SpecialistMcpServer。
        路径基于 Poirot 包安装位置动态解析；extension 文件不存在时
        pi 会报 extension_error，由 _run_rpc_session 捕获为 SpecialistCrashError。
        """
        from poirot.backend.agents.multiagent import __path__ as pkg_path

        import pathlib

        return str(
            pathlib.Path(pkg_path[0])
            / "extensions"
            / "pi-sandbox-bridge"
            / "index.ts"
        )