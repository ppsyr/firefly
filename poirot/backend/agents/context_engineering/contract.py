"""接入契约层：GovernanceStrategy Protocol + GovernanceContext/Result/Metric + MemorySink + helpers。

【整体职责】
这是「策略 bundle」与「StrategyMiddleware adapter」之间的契约层，与具体策略骨架无关。
它定义：
    1. 策略必须实现什么（GovernanceStrategy Protocol，6 个 hook）；
    2. 每次 hook 调用时传什么、返回什么（GovernanceContext / GovernanceResult）；
    3. 策略产出的指标长什么样（GovernanceMetric）；
    4. 短期治理层与长期记忆层的边界（MemorySink Protocol）；
    5. 两个辅助函数：把结果合进 governance（merge_metrics_into_governance）、
       把结果转成 state-channel hook 能消费的 dict patch（apply_governance_result）。

【在架构里的位置】
    StrategyMiddleware（adapter）
        → 构造 GovernanceContext 调 bundle.<hook>()
        → 收 GovernanceResult
        → 用 apply_governance_result 转成 dict patch 返回给 LangGraph

【设计要点】
    - 策略 bundle 内部实现完全自由（协议/schema/执行器不限），只受这 6 个 hook 约束。
    - wrap_model_call / wrap_tool_call 的 handler 由 adapter 持有并调用，
      bundle 只通过 GovernanceContext 收 request/result，不直接持 handler。
    - GovernanceResult 区分「持久」与「请求级」：
        state_patch 持久写 ThreadState；
        request_override 仅替换本次 request payload，不持久。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, runtime_checkable


@dataclass(frozen=True)
class GovernanceContext:
    """hook 级统一入参。策略 bundle 按 hook 取所需字段。

    属性：
        state:           当前 ThreadState（Mapping）。
        governance:      state 里的 governance 子 dict（可能为 None）。
        config:          策略配置（StrategyMiddleware 持有并透传）。
        token_counter:   算 token 的可调用对象，签名 (messages, model_name=None) -> int。
        runtime:         LangGraph runtime。
        hook:            当前 hook 名（如 "before_model" / "wrap_tool_call"）。
        messages:        hook-specific，当前消息列表；不适用时 None。
        tools:           hook-specific，当前工具列表；不适用时 None。
        model_request:   hook-specific，wrap_model_call 的 request；不适用时 None。
        tool_call_request: hook-specific，wrap_tool_call 的 request；不适用时 None。
        tool_result:     hook-specific，wrap_tool_call 的 tool 执行结果；不适用时 None。
    """

    state: Mapping[str, Any]
    governance: dict[str, Any] | None
    config: Any
    token_counter: Callable[[list], int]
    runtime: Any
    hook: str
    # hook-specific（不适用时 None）
    messages: list | None = None
    tools: list | None = None
    model_request: Any | None = None
    tool_call_request: Any | None = None
    tool_result: Any | None = None


@dataclass(frozen=True)
class GovernanceResult:
    """hook 级统一出参。策略通过它把「要写什么」交回 adapter。

    字段语义：
        state_patch:      写 ThreadState（含 governance），持久。
        request_override: 替换 request payload，request-scoped 不持久
                          （wrap_model_call 换 request，wrap_tool_call 换 tool_result）。
        messages_patch:   消息级操作（RemoveMessage / 替换消息）。
        metrics:          本次产出的 GovernanceMetric 列表。
        jump_to:          跳转目标节点（如 "model"）。
    """

    state_patch: dict[str, Any] | None = None
    request_override: Any | None = None
    messages_patch: list | None = None
    metrics: list[GovernanceMetric] = field(default_factory=list)
    jump_to: str | None = None


@dataclass(frozen=True)
class GovernanceMetric:
    """策略级 metric。strategy_name + metric_key 组合前缀写入 governance.metrics。

    属性：
        strategy_name: 策略名（作前缀）。
        metric_key:    指标名（作前缀后缀）。
        value:         指标值。
        run_id:        可选，关联 run。
        thread_id:     可选，关联 thread。

    最终写入 governance.metrics 的键为 ``{strategy_name}.{metric_key}``。
    """

    strategy_name: str
    metric_key: str
    value: int | float
    run_id: str = ""
    thread_id: str = ""


@runtime_checkable
class GovernanceStrategy(Protocol):
    """策略 bundle 接入契约。6 hook（sync + a 前缀 async 对）。

    方法名即 hook 名，不预设能力语义。策略内部协议/schema/执行器完全自由。
    wrap_model_call / wrap_tool_call 由 StrategyMiddleware adapter 包 handler，
    bundle 经 GovernanceContext 收 request/result，不直接持 handler。

    hook 一览：
        before_agent / abefore_agent     ：Agent 启动前后
        after_agent  / aafter_agent      ：Agent 结束前后
        before_model / abefore_model     ：模型调用前
        after_model  / aafter_model      ：模型调用后
        wrap_model_call / awrap_model_call ：包住模型调用（PRE 改 request）
        wrap_tool_call  / awrap_tool_call  ：包住工具调用（POST 改 result）
    """

    def before_agent(self, ctx: GovernanceContext) -> GovernanceResult: ...
    async def abefore_agent(self, ctx: GovernanceContext) -> GovernanceResult: ...

    def after_agent(self, ctx: GovernanceContext) -> GovernanceResult: ...
    async def aafter_agent(self, ctx: GovernanceContext) -> GovernanceResult: ...

    def before_model(self, ctx: GovernanceContext) -> GovernanceResult: ...
    async def abefore_model(self, ctx: GovernanceContext) -> GovernanceResult: ...

    def after_model(self, ctx: GovernanceContext) -> GovernanceResult: ...
    async def aafter_model(self, ctx: GovernanceContext) -> GovernanceResult: ...

    def wrap_model_call(self, ctx: GovernanceContext) -> GovernanceResult: ...
    async def awrap_model_call(self, ctx: GovernanceContext) -> GovernanceResult: ...

    def wrap_tool_call(self, ctx: GovernanceContext) -> GovernanceResult: ...
    async def awrap_tool_call(self, ctx: GovernanceContext) -> GovernanceResult: ...


@runtime_checkable
class MemorySink(Protocol):
    """短期治理层与长期记忆层的边界接口。

    策略在压缩、丢弃消息前，可调用 flush 把「即将被丢的消息」交给长期记忆层，
    避免信息直接消失。

    方法：
        flush(messages_to_drop)  ：同步刷出。
        aflush(messages_to_drop) ：异步刷出。
    """

    def flush(self, messages_to_drop: list) -> None: ...
    async def aflush(self, messages_to_drop: list) -> None: ...


def merge_metrics_into_governance(
    governance: dict[str, Any] | None, metrics: list[GovernanceMetric]
) -> dict[str, Any]:
    """将 GovernanceMetric 列表合并进 governance，按 ``{strategy_name}.{metric_key}`` 前缀写 metrics 子 dict。

    Args:
        governance: 现有 governance（可为 None）。
        metrics:    要合并的 GovernanceMetric 列表。

    Returns:
        新的 governance dict，metrics 子 dict 已按前缀写入。
        - governance 为 None 且 metrics 为空 → 返回 {}。
        - governance 非 None 且 metrics 为空 → 返回其浅拷贝。
        - 已有 metrics 子 dict 会保留，新指标按前缀覆盖/新增。

    说明：
        只做浅拷贝（dict(governance)），不深拷贝嵌套结构。
    """
    if not metrics:
        return dict(governance) if governance else {}
    g: dict[str, Any] = dict(governance) if governance else {}
    metrics_dict: dict[str, Any] = dict(g.get("metrics") or {})
    for m in metrics:
        metrics_dict[f"{m.strategy_name}.{m.metric_key}"] = m.value
    g["metrics"] = metrics_dict
    return g


def apply_governance_result(
    state: Mapping[str, Any], result: GovernanceResult
) -> dict[str, Any] | None:
    """将 GovernanceResult 转为 state-channel hook 返回的 dict patch。

    适用于 before/after_agent、before/after_model（返回 dict 的 hook）。
    wrap_model_call/wrap_tool_call 的 request_override 由 StrategyMiddleware adapter inline 处理，
    不经过本函数。

    Args:
        state:  当前 ThreadState（用于取 base governance）。
        result: 策略返回的 GovernanceResult。

    Returns:
        可被 LangGraph state-channel hook 消费的 dict patch；无任何内容时返回 None。

    组装规则：
        1. 以 result.state_patch 为基础（浅拷贝）。
        2. messages_patch 存在 → patch["messages"] = messages_patch。
        3. metrics 存在 → 取 patch 里已有的 governance 或 state 里的 governance 作 base，
           调 merge_metrics_into_governance 合并后写回 patch["governance"]。
        4. jump_to 存在 → patch["jump_to"] = jump_to。
        5. 最终 patch 为空 dict → 返回 None。
    """
    patch: dict[str, Any] = dict(result.state_patch or {})
    if result.messages_patch:
        patch["messages"] = result.messages_patch
    if result.metrics:
        base_gov = patch.get("governance", state.get("governance"))
        patch["governance"] = merge_metrics_into_governance(base_gov, result.metrics)
    if result.jump_to:
        patch["jump_to"] = result.jump_to
    return patch or None