"""StrategyMiddleware — 接入 adapter，把 6 个 hook 路由到 GovernanceStrategy bundle。

【整体职责】
用一个 AgentMiddleware 持有一个策略 bundle 实例，把 LangChain 暴露的 6 类 hook
（before_agent / after_agent / before_model / after_model / wrap_model_call /
wrap_tool_call）统一路由到 bundle 上对应的方法。

【内容摘要】
- StrategyMiddleware           : 接入 adapter，6 hook 路由到 bundle。
- StrategyMiddleware._ctx      : 构造 GovernanceContext，供 bundle 侧使用。
- before_agent / abefore_agent / after_agent / aafter_agent
- before_model / abefore_model / after_model / aafter_model（标 can_jump_to=["model"]）
- wrap_model_call / awrap_model_call（PRE：bundle 改 request）
- wrap_tool_call / awrap_tool_call（POST：bundle 改 result）

【职责边界】
- 只负责：把 6 类 hook 路由到 bundle，应用其返回的治理结果。
- 不负责：治理策略的具体实现（bundle）、state 结构（state/types）、
  治理结果的语义（context_engineering/contract）。

【INVARIANT】
- 两类 hook 语义不同：
  - state-channel hooks（before/after_agent、before/after_model）：返回 dict
    （state_patch），经 apply_governance_result 应用到 state；可携带 metrics /
    jump_to；before/after_model 标 @hook_config(can_jump_to=["model"])。
  - wrap hooks（wrap_model_call / wrap_tool_call）：adapter 包住 handler，
    只消费 GovernanceResult.request_override；无 state 通道，状态写入交给
    before/after hook。
- wrap_model_call = PRE（bundle 先改 request，再交给 handler）。
- wrap_tool_call  = POST（先取 tool_result，再让 bundle 改 result）。
- ctx 统一注入：state / governance / config / token_counter / runtime / hook
  + hook 特定参数。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, override

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelCallResult,
    ModelRequest,
    ModelResponse,
    hook_config,
)
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.runtime import Runtime
from langgraph.types import Command

from poirot.backend.agents.context_engineering.contract import (
    GovernanceContext,
    GovernanceResult,
    GovernanceStrategy,
    apply_governance_result,
)
from poirot.backend.agents.context_engineering.utilities import token_counter
from poirot.backend.agents.state.types import ThreadState


class StrategyMiddleware(AgentMiddleware):
    """接入 adapter：把 6 个 hook 路由到 GovernanceStrategy bundle。

    持有一个 bundle 实例（同步 + 异步方法齐全），所有 hook 都通过
    self._bundle.<hook_name>(ctx) 调用，返回值经 apply_governance_result
    或 request_override 应用。

    Attributes:
        _bundle: 治理策略 bundle，提供 6 类 hook 的同步/异步实现。
        _config: 透传给 GovernanceContext 的配置对象。
    """

    state_schema = ThreadState  # type: ignore[assignment]

    def __init__(self, bundle: GovernanceStrategy, config: Any = None) -> None:
        """初始化。

        Args:
            bundle: 治理策略 bundle，提供 6 类 hook 的同步/异步实现。
            config: 透传给 GovernanceContext 的配置对象，可为 None。
        """
        self._bundle = bundle
        self._config = config

    def _ctx(
        self,
        state: ThreadState,
        runtime: Runtime,
        hook: str,
        **hook_specific: Any,
    ) -> GovernanceContext:
        """构造 GovernanceContext，供 bundle 侧各 hook 使用。

        统一注入的公共字段：state、governance（取自 state）、config、
        token_counter、runtime、hook 名。hook_specific 用于传该 hook
        特有的参数（如 messages、model_request、tool_call_request、
        tool_result、tools 等），由调用方按需给出。

        Args:
            state: 当前 ThreadState。
            runtime: LangGraph 运行时。
            hook: 当前 hook 名称（如 "before_model" / "wrap_tool_call"）。
            **hook_specific: 该 hook 特有的额外字段。

        Returns:
            GovernanceContext: 填充好的治理上下文。
        """
        return GovernanceContext(
            state=state,
            governance=state.get("governance"),
            config=self._config,
            token_counter=token_counter,
            runtime=runtime,
            hook=hook,
            **hook_specific,
        )

    # ------------------------------------------------------------------
    # state-channel hooks：返回 dict，经 apply_governance_result 应用到 state
    # ------------------------------------------------------------------

    @override
    def before_agent(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:
        """同步 before_agent hook：调 bundle.before_agent，应用治理结果。

        Args:
            state: 当前 ThreadState。
            runtime: LangGraph 运行时。

        Returns:
            dict[str, Any] | None: apply_governance_result 处理后的 state patch（或 None）。
        """
        return apply_governance_result(state, self._bundle.before_agent(self._ctx(state, runtime, "before_agent", messages=state.get("messages") or [])))

    @override
    async def abefore_agent(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:
        """异步 before_agent hook：调 bundle.abefore_agent，应用治理结果。"""
        result = await self._bundle.abefore_agent(self._ctx(state, runtime, "before_agent", messages=state.get("messages") or []))
        return apply_governance_result(state, result)

    @override
    def after_agent(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:
        """同步 after_agent hook：调 bundle.after_agent，应用治理结果。

        Args:
            state: 当前 ThreadState。
            runtime: LangGraph 运行时。

        Returns:
            dict[str, Any] | None: apply_governance_result 处理后的 state patch（或 None）。
        """
        return apply_governance_result(state, self._bundle.after_agent(self._ctx(state, runtime, "after_agent", messages=state.get("messages") or [])))

    @override
    async def aafter_agent(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:
        """异步 after_agent hook：调 bundle.aafter_agent，应用治理结果。"""
        result = await self._bundle.aafter_agent(self._ctx(state, runtime, "after_agent", messages=state.get("messages") or []))
        return apply_governance_result(state, result)

    @hook_config(can_jump_to=["model"])
    @override
    def before_model(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:
        """同步 before_model hook：调 bundle.before_model，应用治理结果。

        标了 @hook_config(can_jump_to=["model"])：治理结果允许跳回 model 节点。

        Args:
            state: 当前 ThreadState。
            runtime: LangGraph 运行时。

        Returns:
            dict[str, Any] | None: apply_governance_result 处理后的 state patch（或 None）。
        """
        return apply_governance_result(state, self._bundle.before_model(self._ctx(state, runtime, "before_model", messages=state.get("messages") or [])))

    @hook_config(can_jump_to=["model"])
    @override
    async def abefore_model(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:
        """异步 before_model hook：调 bundle.abefore_model，应用治理结果（可跳回 model）。"""
        result = await self._bundle.abefore_model(self._ctx(state, runtime, "before_model", messages=state.get("messages") or []))
        return apply_governance_result(state, result)

    @hook_config(can_jump_to=["model"])
    @override
    def after_model(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:
        """同步 after_model hook：调 bundle.after_model，应用治理结果。

        标了 @hook_config(can_jump_to=["model"])：治理结果允许跳回 model 节点。

        Args:
            state: 当前 ThreadState。
            runtime: LangGraph 运行时。

        Returns:
            dict[str, Any] | None: apply_governance_result 处理后的 state patch（或 None）。
        """
        return apply_governance_result(state, self._bundle.after_model(self._ctx(state, runtime, "after_model", messages=state.get("messages") or [])))

    @hook_config(can_jump_to=["model"])
    @override
    async def aafter_model(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:
        """异步 after_model hook：调 bundle.aafter_model，应用治理结果（可跳回 model）。"""
        result = await self._bundle.aafter_model(self._ctx(state, runtime, "after_model", messages=state.get("messages") or []))
        return apply_governance_result(state, result)

    # ------------------------------------------------------------------
    # wrap hooks：adapter 包住 handler，仅消费 GovernanceResult.request_override
    #
    # 注意：wrap hook 不适用 state_patch / metrics —— wrap 必须返回
    # ModelCallResult / ToolMessage，没有 state 通道。状态写入走上面的
    # before/after hook。
    # ------------------------------------------------------------------

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        """同步 wrap_model_call（PRE）：bundle 先改 request，再交给 handler。

        流程：
        1. 从 request.runtime.state 取 state（取不到用空 dict）。
        2. 构造 ctx，带上 model_request / messages / tools 等 hook 特定参数。
        3. 调 bundle.wrap_model_call(ctx) 得到 GovernanceResult。
        4. 若 result.request_override 非 None，用它替换 request；否则用原 request。
        5. 调 handler(req) 并返回。

        Args:
            request: 原始模型调用请求。
            handler: 下游处理函数。

        Returns:
            ModelCallResult: handler 返回的模型调用结果。
        """
        state = getattr(getattr(request, "runtime", None), "state", None) or {}
        ctx = self._ctx(state, getattr(request, "runtime", None), "wrap_model_call", model_request=request, messages=getattr(request, "messages", None) or [], tools=getattr(request, "tools", None))
        result = self._bundle.wrap_model_call(ctx)
        req = result.request_override if result.request_override is not None else request
        return handler(req)

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        """异步 wrap_model_call（PRE）：bundle 先改 request，再 await handler。"""
        state = getattr(getattr(request, "runtime", None), "state", None) or {}
        ctx = self._ctx(state, getattr(request, "runtime", None), "wrap_model_call", model_request=request, messages=getattr(request, "messages", None) or [], tools=getattr(request, "tools", None))
        result = await self._bundle.awrap_model_call(ctx)
        req = result.request_override if result.request_override is not None else request
        return await handler(req)

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        """同步 wrap_tool_call（POST）：先取 tool result，再让 bundle 改 result。

        与 wrap_model_call 的方向相反：
        1. 先调 handler(request) 拿到 tool_result。
        2. 从 request.runtime.state 取 state。
        3. 构造 ctx，带上 tool_call_request / tool_result。
        4. 调 bundle.wrap_tool_call(ctx) 得到 GovernanceResult。
        5. 若 result.request_override 非 None，返回它；否则返回原 tool_result。

        Args:
            request: 工具调用请求。
            handler: 下游处理函数，返回 ToolMessage 或 Command。

        Returns:
            ToolMessage | Command: 改写后的 ToolMessage / Command（或原 tool_result）。
        """
        tool_result = handler(request)
        state = getattr(getattr(request, "runtime", None), "state", None) or {}
        ctx = self._ctx(state, getattr(request, "runtime", None), "wrap_tool_call", tool_call_request=request, tool_result=tool_result)
        result = self._bundle.wrap_tool_call(ctx)
        return result.request_override if result.request_override is not None else tool_result

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        """异步 wrap_tool_call（POST）：先 await handler 取 result，再让 bundle 改 result。"""
        tool_result = await handler(request)
        state = getattr(getattr(request, "runtime", None), "state", None) or {}
        ctx = self._ctx(state, getattr(request, "runtime", None), "wrap_tool_call", tool_call_request=request, tool_result=tool_result)
        result = await self._bundle.awrap_tool_call(ctx)
        return result.request_override if result.request_override is not None else tool_result