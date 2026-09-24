"""上下文工程治理层。

【整体职责】
context_engineering 是 Agent 的「上下文治理框架」：它不直接实现治理逻辑，
而是提供一套契约、注册表与装配器，让「策略 bundle」以可插拔的方式接入。
治理的核心关注点：token 预算、上下文压缩、工具结果外化、压缩前快照、摘要。

【本包对外暴露的能力】
1. 6 能力 Protocol（GovernanceStrategy）
   - 6 对 hook（sync + async）：before/after_agent、before/after_model、
     wrap_model_call、wrap_tool_call。
   - 策略 bundle 实现这 6 hook，内部协议/schema/执行器完全自由。

2. 多 middleware 接入
   - 公共 2 固定：TaggedContextMiddleware（渲染 XML 标签上下文）、
     MessageNormalizerMiddleware（合并多 SystemMessage 为单条 leading）。
   - 策略 1：StrategyMiddleware（接入 adapter，路由 6 hook 到 bundle）。
   - 由 builder.build_governance_middlewares 统一装配。

3. 统一 GovernanceState 共享字段
   - governance 挂在 ThreadState 下，策略通过 state_patch 持久写入。
   - DefaultStrategy 约定 governance["default"] 下存：
     budget / pending / seen_msgs / warned / p1_completed /
     p1_skip_until_fraction / summary / summary_id /
     snapshot_path / p3_obs_limit / metrics 等。

4. 能力实现注册表
   - registry.register_strategy(name)：装饰器注册策略 bundle 类。
   - registry.get_strategy_class(name)：按名查表，未注册抛 KeyError。
   - builder 导入 strategies 触发注册（import 副作用）。

5. config 预置 strategy
   - ContextGovernanceConfig.strategy 指定策略名（如 "default"）。
   - ContextGovernanceConfig.params 透传给策略 bundle 构造函数。
   - 策略未注册时降级为「仅公共 2 个」，不崩。

【目录结构】
    __init__.py            本文件，包入口
    builder.py             装配器：build_governance_middlewares
    contract.py            接入契约：GovernanceStrategy / Context / Result / Metric
    registry.py            策略 bundle 注册表
    strategy_middleware.py 接入 adapter：StrategyMiddleware
    utilities.py           token 工具：token_counter / resolve_window_size / resolve_model_name
    strategies/            策略实现目录
        default/           默认策略（@register_strategy("default")）
            _constants.py   CST 时区常量
            budget.py       BudgetTrackerExecutor（预算追踪）
            externalizer.py ExternalizerExecutor（工具结果外化）
            snapshot.py     SnapshotExecutor（P4 快照）
            summarizer.py   SummarizerExecutor（P4 摘要）
            strategy.py     DefaultStrategy（6 hook 实现）

【典型调用链】
    上层 Agent 组装
        → builder.build_governance_middlewares(config, model, summarize_model)
            → registry.get_strategy_class(config.strategy)
            → bundle_cls(config.params, model, summarize_model)
            → StrategyMiddleware(bundle, config.params)
        → 返回 list[AgentMiddleware] 挂进 Agent
    运行期
        → StrategyMiddleware.<hook> → bundle.<hook>(GovernanceContext)
        → GovernanceResult → apply_governance_result → dict patch
"""

from __future__ import annotations

# 触发策略 bundle 注册（import 副作用）：
# 使 strategies/ 下的 @register_strategy 装饰器在包导入时执行，
# 后续 registry.get_strategy_class("default") 才能查到 DefaultStrategy。
from poirot.backend.agents.context_engineering import strategies  # noqa: F401