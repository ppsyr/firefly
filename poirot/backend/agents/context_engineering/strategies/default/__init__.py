"""default 策略子包。

【整体职责】
本子包是「默认上下文治理策略」的实现，对应注册名 "default"。
它实现 GovernanceStrategy 的 6 对 hook，按 token 占用 fraction 分阶段
（P1/P2/P3/P4/P5）触发上下文压缩动作。

【内部组成】
    _constants.py   CST 时区常量（UTC+8），所有 compaction 产物统一时间口径
    budget.py       BudgetTrackerExecutor（预算追踪：init_budget / track / clear_run_state）
    externalizer.py ExternalizerExecutor（工具结果外化：单条 + 批量 FIFO）
    snapshot.py     SnapshotExecutor（P4 压缩前存快照）
    summarizer.py   SummarizerExecutor（P4 用 LLM 全量摘要旧消息）
    strategy.py     DefaultStrategy（策略主体，@register_strategy("default")）

【导入即注册】
    default/__init__.py
        └─ from ...strategies.default.strategy import DefaultStrategy
              └─ strategy.py 中 @register_strategy("default")
                    → registry._STRATEGY_BUNDLES["default"] = DefaultStrategy

    strategies/__init__.py 会 import 本子包，因此只要上层 import
    context_engineering（或 strategies），注册就会自动完成。

【与 registry / builder 的关系】
    - registry.get_strategy_class("default") 查到的就是这里的 DefaultStrategy。
    - builder.build_governance_middlewares(config, ...) 按 config.strategy 查表，
      实例化 DefaultStrategy(config.params, model, summarize_model)，
      再包成 StrategyMiddleware(bundle, config.params) 挂进 Agent。

【实例化参数（config.params 透传）】
    thresholds / externalize_dir / externalize_min_chars /
    externalize_preview_chars / exempt_rounds / tool_metadata /
    preserve_recent / snapshot_dir / summarize_model
"""

from __future__ import annotations

# 导入即触发 strategy.py 里的 @register_strategy("default") 注册
from poirot.backend.agents.context_engineering.strategies.default.strategy import DefaultStrategy

__all__ = ["DefaultStrategy"]