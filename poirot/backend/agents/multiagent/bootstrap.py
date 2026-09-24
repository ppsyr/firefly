"""Multi-Agent bootstrap — 装配 specialist + 凭证检测 + metrics + 注入 CapabilityRegistry。

【整体职责】
multiagent 的装配入口：按 config 反射加载 specialist、检测凭证、构造 metrics /
middleware / tools，并把结果打包成 MultiAgentSetup 交给 CapabilityRegistry 注入。
enabled=false 时返回空 setup，Leader 行为不变。

【内容摘要】
- _warn_specialist_disabled() : 凭证缺失时按 specialist 类型打印启用指引（不阻塞主流程）。
- MultiAgentSetup(frozen)     : 装配结果容器（registry / subagent / metrics / middleware / tools / l2）。
- _EMPTY_SETUP                : enabled=false 时返回的空 setup。
- _load_specialist()          : 反射加载单个 specialist + 凭证检测 + 匹配 summarizer。
- setup_multiagent()          : 装配主入口。

【职责边界】
- 只负责：反射加载、凭证检测、构造 metrics/middleware/tools、打包 setup、warn 提示。
- 不负责：运行时派活（Leader / runtime 负责）、L2 内部装配（evolution.bootstrap 负责）、
  specialist 实际执行。
- 不持有状态：函数式装配，产出 setup 后即退出。

【INVARIANT】
- enabled=false → 返回 _EMPTY_SETUP，Leader 行为完全不变。
- 凭证缺失 → specialist disabled（不注册、不生成 tool），打印 warning，不抛异常。
- subagent 凭证缺失视为 bug，打印 error 级别（subagent 应零配置可用）。
- subagent 特殊处理：若已在 specialists_use 中则不再单独生成 delegate_to_subagent 工具，
  避免重复。
- L2 未启用时 l2_setup=None，L1 行为不变（向后兼容）。
- setup 返回后由上层（CapabilityRegistry）注入，本模块不做全局单例。

【已知的返回值不一致】
- MultiAgentSetup.subagent_provider 声明为 SubagentRuntime | None，
  但 _load_specialist 返回的 subagent 分支中 runtime 未被单独提取；
  setup_multiagent 中仅在 "subagent" 不在 specialists_use 时才构造 subagent_provider。
- _load_specialist 的返回值类型标注为 tuple[Any, Any, Any] | None，
  第三个元素 result_summarizer 在 subagent 分支实际未被使用（仅用于统一接口）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from langchain_core.tools import BaseTool

from poirot.backend.agents.multiagent.config import MultiAgentConfig
from poirot.backend.agents.multiagent.middleware import OrchestrationMiddleware
from poirot.backend.agents.multiagent.metrics import MultiAgentMetricsStore
from poirot.backend.agents.multiagent.registry import SpecialistRegistry
from poirot.backend.agents.multiagent.runtimes.subagent_runtime import SubagentRuntime
from poirot.backend.agents.multiagent.tools import (
    make_specialist_tool,
    make_subagent_tool,
)

logger = logging.getLogger(__name__)


def _warn_specialist_disabled(name: str, reason: str) -> None:
    """按 specialist 类型打印"如何启用"的指引，不阻塞主流程。

    - pi / codex / claude：warning 级别 + 安装与登录步骤。
    - subagent：error 级别（subagent 应零配置可用，disabled 说明是 bug）。
    - 其他：warning 级别 + 简要原因。
    """
    if name == "pi":
        logger.warning(
            "[PiSpecialist] disabled: %s\n"
            "To enable, install Pi CLI and set any API key:\n"
            "  npm install -g @earendil-works/pi-coding-agent\n"
            "  Set any of: ANTHROPIC_API_KEY, OPENAI_API_KEY, DEEPSEEK_API_KEY, "
            "KIMI_API_KEY, MINIMAX_API_KEY, etc.\n"
            "Or configure multiagent.specialists.pi.provider in config.yaml",
            reason,
        )
    elif name == "codex":
        logger.warning(
            "[CodexSpecialist] disabled: %s\n"
            "To enable, install Codex CLI and login:\n"
            "  npm install -g @openai/codex\n"
            "  codex login\n"
            "Or set CODEX_AUTH_PATH env var pointing to auth.json",
            reason,
        )
    elif name == "claude":
        logger.warning(
            "[ClaudeCodeSpecialist] disabled: %s\n"
            "To enable, install Claude Code CLI and login:\n"
            "  npm install -g @anthropic/claude-code\n"
            "  claude /login\n"
            "Or set CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_AUTH_TOKEN env var",
            reason,
        )
    elif name == "subagent":
        logger.error(
            "[SubagentSpecialist] disabled: %s\n"
            "This is a bug — subagent should be zero-config. "
            "Check that agent_factory is injected in bootstrap.",
            reason,
        )
    else:
        logger.warning(
            "[Specialist:%s] disabled: %s", name, reason
        )


@dataclass(frozen=True)
class MultiAgentSetup:
    """setup_multiagent 的返回结果，供 CapabilityRegistry 注入。

    全部字段在 L2 未启用时可能为 None / 空，调用方需判空。
    """

    specialist_registry: SpecialistRegistry | None
    subagent_provider: SubagentRuntime | None
    metrics_store: MultiAgentMetricsStore | None
    orchestration_middleware: OrchestrationMiddleware | None
    specialist_tools: tuple[BaseTool, ...]
    # L2 进化层装配结果；config.l2.enabled=false 时为 None
    l2_setup: Any | None = None


# enabled=false 时返回的空 setup：所有字段为空，Leader 行为不变。
_EMPTY_SETUP = MultiAgentSetup(
    specialist_registry=None,
    subagent_provider=None,
    metrics_store=None,
    orchestration_middleware=None,
    specialist_tools=(),
    l2_setup=None,
)


def _load_specialist(
    name: str,
    config: MultiAgentConfig,
    agent_factory: Callable[[], Any] | None = None,
) -> tuple[Any, Any, Any] | None:
    """反射加载单个 specialist + 凭证检测 + 匹配 summarizer。

    返回 (specialist, context_summarizer, result_summarizer) 三元组；
    返回 None 表示 specialist disabled（凭证缺失或未知 name）。
    """
    if name == "pi":
        from poirot.backend.agents.multiagent.installer.pi_installer import (
            PiInstaller,
        )
        from poirot.backend.agents.multiagent.credentials.pi_credential import (
            PiCredentialProvider,
        )

        # 确保 pi 已装（后台安装不阻塞）
        installer = PiInstaller(
            auto_install=config.specialists_pi_auto_install
        )
        if not installer.ensure_installed():
            return None  # pi 不可用，disabled（后台安装中或不可装）

        # 双轨凭证解析（config 优先 + env 兜底）
        cred_provider = PiCredentialProvider(
            config_provider=config.specialists_pi_provider or None,
            config_api_key=config.specialists_pi_api_key or None,
        )
        cred = cred_provider.get_credential()
        if cred is None:
            return None  # 凭证缺失，disabled

        # 加载 PiSpecialist（PiRuntime 内部 --no-builtin-tools + extension）
        from poirot.backend.agents.multiagent.runtimes.pi_runtime import (
            PiRuntime,
            PiRuntimeConfig,
        )
        from poirot.backend.agents.multiagent.specialists.pi_specialist import (
            PiSpecialist,
        )
        from poirot.backend.agents.multiagent.summarizers.context.pi_context_summarizer import (
            PiContextSummarizer,
        )
        from poirot.backend.agents.multiagent.summarizers.result.pi_result_summarizer import (
            PiResultSummarizer,
        )

        runtime_config = PiRuntimeConfig(
            provider=cred.provider,
            model=config.specialists_pi_model or None,
            thinking_level=config.specialists_pi_thinking_level,
        )
        return (
            PiSpecialist(runtime=PiRuntime(config=runtime_config), credential=cred),
            PiContextSummarizer(),
            PiResultSummarizer(),
        )

    if name == "codex":
        from poirot.backend.agents.multiagent.credentials.codex_credential import (
            CodexCredentialProvider,
        )
        cred = CodexCredentialProvider().get_credential()
        if cred is None:
            return None
        from poirot.backend.agents.multiagent.specialists.codex_specialist import (
            CodexSpecialist,
        )
        from poirot.backend.agents.multiagent.summarizers.context.codex_context_summarizer import (
            CodexContextSummarizer,
        )
        from poirot.backend.agents.multiagent.summarizers.result.codex_result_summarizer import (
            CodexResultSummarizer,
        )
        return CodexSpecialist(), CodexContextSummarizer(), CodexResultSummarizer()

    if name == "claude":
        from poirot.backend.agents.multiagent.credentials.claude_credential import (
            ClaudeCredentialProvider,
        )
        cred = ClaudeCredentialProvider().get_credential()
        if cred is None:
            return None
        from poirot.backend.agents.multiagent.specialists.claude_code_specialist import (
            ClaudeCodeSpecialist,
        )
        from poirot.backend.agents.multiagent.summarizers.context.claude_code_context_summarizer import (
            ClaudeCodeContextSummarizer,
        )
        from poirot.backend.agents.multiagent.summarizers.result.claude_code_result_summarizer import (
            ClaudeCodeResultSummarizer,
        )
        return (
            ClaudeCodeSpecialist(),
            ClaudeCodeContextSummarizer(),
            ClaudeCodeResultSummarizer(),
        )

    if name == "subagent":
        from poirot.backend.agents.multiagent.runtimes.subagent_runtime import (
            SubagentRuntime,
        )
        from poirot.backend.agents.multiagent.specialists.subagent_specialist import (
            SubagentSpecialist,
        )
        from poirot.backend.agents.multiagent.summarizers.context.self_copy_context_summarizer import (
            SelfCopyContextSummarizer,
        )
        from poirot.backend.agents.multiagent.summarizers.result.self_copy_result_summarizer import (
            SelfCopyResultSummarizer,
        )
        runtime = SubagentRuntime(agent_factory=agent_factory) if agent_factory else SubagentRuntime()
        return (
            SubagentSpecialist(runtime=runtime),
            SelfCopyContextSummarizer(),
            SelfCopyResultSummarizer(),
        )

    return None


def setup_multiagent(
    config: MultiAgentConfig,
    agent_factory: Callable[[], Any] | None = None,
) -> MultiAgentSetup:
    """装配 multi-agent orchestration。

    - enabled=false → 返回 _EMPTY_SETUP，Leader 行为不变。
    - enabled=true → 反射加载 specialist + 凭证检测 + metrics + middleware + tools。
    """
    if not config.enabled:
        return _EMPTY_SETUP

    metrics = MultiAgentMetricsStore(config.metrics_db_path)

    # L2 进化层装配（enabled=false → l2_setup=None，L1 行为不变）
    from poirot.backend.agents.multiagent.evolution.bootstrap import setup_l2
    l2_setup = setup_l2(config, metrics)

    orch_mw = OrchestrationMiddleware(
        metrics_store=metrics,
        l2_trigger_middleware=l2_setup.l2_trigger_middleware if l2_setup else None,
        budget_guard=l2_setup.budget_guard if l2_setup else None,
    )
    specialist_registry = SpecialistRegistry()
    tools: list[BaseTool] = []

    for name in config.specialists_use:
        loaded = _load_specialist(name, config, agent_factory=agent_factory)
        if loaded is None:
            _warn_specialist_disabled(name, "credential missing or load failed")
            continue
        specialist, ctx_summarizer, result_summarizer = loaded
        specialist_registry.register(specialist)
        tools.append(
            make_specialist_tool(
                name,
                specialist,
                ctx_summarizer,
                result_summarizer,
                max_steps=config.max_steps,
                timeout_seconds=config.timeout_seconds,
                version_dag=l2_setup.version_dag if l2_setup else None,
                budget_guard=l2_setup.budget_guard if l2_setup else None,
            )
        )

    subagent_provider: SubagentRuntime | None = None
    if agent_factory is not None and "subagent" not in config.specialists_use:
        # 仅当 subagent 未作为 specialist 注册时，才单独生成 delegate_to_subagent 工具
        subagent_provider = SubagentRuntime(agent_factory=agent_factory)
        from poirot.backend.agents.multiagent.summarizers.context.self_copy_context_summarizer import (
            SelfCopyContextSummarizer,
        )
        from poirot.backend.agents.multiagent.summarizers.result.self_copy_result_summarizer import (
            SelfCopyResultSummarizer,
        )
        tools.append(
            make_subagent_tool(
                subagent_provider,
                SelfCopyContextSummarizer(),
                SelfCopyResultSummarizer(),
                max_steps=config.subagent_max_steps,
                timeout_seconds=config.subagent_timeout_seconds,
            )
        )

    # L2 启用时启动 daemon 线程
    if l2_setup is not None:
        l2_setup.worker.start()

    return MultiAgentSetup(
        specialist_registry=specialist_registry,
        subagent_provider=subagent_provider,
        metrics_store=metrics,
        orchestration_middleware=orch_mw,
        specialist_tools=tuple(tools),
        l2_setup=l2_setup,
    )