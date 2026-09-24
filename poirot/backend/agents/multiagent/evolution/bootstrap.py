"""L2 evolution bootstrap — 装配 L2 组件 + 启动 daemon 线程。

【整体职责】
L2 进化层的装配入口：当 config.l2.enabled=true 时，构造 L2 的全部组件
（VersionDAG / PromotionGate / EvolutionMutator / TriggerManager /
L2TriggerMiddleware / BudgetGuard / OrchestrationMetricsL2 / L2EvolutionWorker），
并在 L3 启用时嵌套调用 setup_l3；enabled=false 时返回 None，L1 行为不变。

【内容摘要】
- L2Setup(dataclass)                 : L2 装配结果容器（注入 L1 MultiAgentSetup）。
- setup_l2(config, metrics_store)    : 装配主入口。
- _build_budget_limits(budget_config): 从 BudgetConfig 构造 per-specialist BudgetLimit。

【职责边界】
- 只负责：构造 L2 组件、注入 L3（可选）、打包 L2Setup。
- 不负责：L2 演化逻辑（各组件负责）、L3 内部装配（setup_l3 负责）。
- 不持有状态：装配产出 L2Setup 后返回。

【INVARIANT】
- enabled=false → 返回 None，L1 行为完全不变。
- L2 schema 由 MultiAgentMetricsStore 初始化（v1→v2），VersionDAG 复用同一 db（Z3 模式）。
- metrics_view = metrics_store：L1 MultiAgentMetricsStore 实现 MetricsView Protocol。
- MVP 阶段 EvolutionMutator 的 llm_caller=None（数据驱动触发后才注入真实 LLM）。
- MVP 阶段 PromotionGate 的 evaluator=None（floor eval）。
- L3 启用时注入 bridge（OrchestrationBridge）到 PromotionGate；否则 bridge=None。
- task_queue 是 L2 与 L3 共享的 cron queue。
- BudgetGuard 从 BudgetConfig 转成 BudgetLimit dict。
- L3 启用时嵌套调用 setup_l3(config, metrics_store, task_queue)。
- worker 是 daemon 线程，由 setup 时构造（启动由外部触发）。
"""
from __future__ import annotations

import queue
from dataclasses import dataclass
from typing import Any

from poirot.backend.agents.multiagent.config import BudgetConfig, MultiAgentConfig
from poirot.backend.agents.multiagent.evolution.budget_guard import (
    BudgetGuard,
    BudgetLimit,
)
from poirot.backend.agents.multiagent.evolution.evolution_mutator import EvolutionMutator
from poirot.backend.agents.multiagent.evolution.failure_focuser import FailureFocuser
from poirot.backend.agents.multiagent.evolution.metrics_l2 import OrchestrationMetricsL2
from poirot.backend.agents.multiagent.evolution.metrics_view import MetricsView
from poirot.backend.agents.multiagent.evolution.promotion_gate import PromotionGate
from poirot.backend.agents.multiagent.evolution.trigger_manager import (
    TriggerManager,
    TriggerThresholds,
)
from poirot.backend.agents.multiagent.evolution.trigger_middleware import L2TriggerMiddleware
from poirot.backend.agents.multiagent.evolution.version_dag import VersionDAG
from poirot.backend.agents.multiagent.evolution.worker import L2EvolutionWorker


@dataclass
class L2Setup:
    """L2 装配结果——注入 L1 MultiAgentSetup。

    持有 L2 全部运行时组件 + 共享 task_queue；L3 未启用时 l3_setup=None。
    """

    version_dag: VersionDAG
    promotion_gate: PromotionGate
    evolution_mutator: EvolutionMutator
    trigger_manager: TriggerManager
    l2_trigger_middleware: L2TriggerMiddleware
    budget_guard: BudgetGuard
    metrics_l2: OrchestrationMetricsL2
    worker: L2EvolutionWorker
    task_queue: "queue.Queue"
    # L3 eval setup（config.l3.enabled=false 时为 None，L2 行为不变）
    l3_setup: Any = None


def setup_l2(
    config: MultiAgentConfig,
    metrics_store: Any,
) -> L2Setup | None:
    """装配 L2 进化层。

    - config.l2.enabled=false → 返回 None（L1 行为不变）。
    - config.l2.enabled=true → 构造全部 L2 组件 + 启动 daemon 线程。

    装配顺序：
    1. VersionDAG（复用 metrics_db_path）。
    2. metrics_view = metrics_store（L1 实现 MetricsView Protocol）。
    3. FailureFocuser（24h 窗口）。
    4. EvolutionMutator（MVP llm_caller=None）。
    5. PromotionGate（L3 启用时注入 bridge）。
    6. TriggerManager + task_queue。
    7. L2TriggerMiddleware。
    8. BudgetGuard。
    9. OrchestrationMetricsL2。
    10. L2EvolutionWorker。
    11. L3 启用时嵌套 setup_l3。
    12. 打包 L2Setup。
    """
    if not config.l2.enabled:
        return None

    # L2 schema 已由 MultiAgentMetricsStore 初始化（v1→v2 迁移）。
    # VersionDAG 用同一 db path（Z3 模式）。
    version_dag = VersionDAG(db_path=config.metrics_db_path)

    # MetricsView：L1 MultiAgentMetricsStore 实现此 Protocol
    metrics_view = metrics_store  # 运行时做 isinstance 检查

    # FailureFocuser（默认 24h 窗口）
    focuser = FailureFocuser(
        window_seconds=config.l2.failure_window_hours * 3600.0
    )

    # EvolutionMutator（MVP llm_caller=None，真实 LLM 后续注入）
    mutator = EvolutionMutator(
        llm_caller=None,
        evolution_model=config.l2.evolution_model,
        max_retries=2,
    )

    # PromotionGate（MVP evaluator=None，floor eval）
    # L3 启用时注入 bridge（lazy import 避免 L2→L3 静态依赖）
    bridge: Any = None
    if config.l3.enabled:
        from poirot.backend.agents.multiagent.eval.bridge import OrchestrationBridge
        from poirot.backend.agents.multiagent.eval.registry import SpecialistEvalRegistry
        from poirot.backend.agents.multiagent.eval.adapters.programmatic import ProgrammaticAdapter
        from poirot.backend.agents.multiagent.eval.adapters.llm_judge import LLMJudgeAdapter
        from poirot.backend.agents.multiagent.eval.adapters.longitudinal_pairs import LongitudinalPairsAdapter
        registry = SpecialistEvalRegistry()
        registry.register("programmatic", ProgrammaticAdapter(evaluator=None))
        registry.register("llm_judge", LLMJudgeAdapter(judge_fn=None))
        registry.register("longitudinal_pairs", LongitudinalPairsAdapter(evaluator=None))
        bridge = OrchestrationBridge(adapter_registry=registry)

    gate = PromotionGate(
        evaluator=None,
        version_dag=version_dag,
        eval_timeout_seconds=config.l2.eval_timeout_seconds,
        eval_sample_min=config.l2.eval_sample_min,
        eval_sample_max=config.l2.eval_sample_max,
        eval_task_max_reuse=config.l2.eval_task_max_reuse,
        bridge=bridge,
    )

    # TriggerManager（4 源 + 冷却 + per-profile 串行）
    task_queue: queue.Queue = queue.Queue()
    thresholds = TriggerThresholds(
        failure_window_seconds=config.l2.failure_window_hours * 3600.0,
        failure_threshold=config.l2.failure_threshold,
        degradation_min_invoked=config.l2.degradation_min_invoked,
        degradation_threshold=config.l2.degradation_threshold,
        cost_alert_usd=config.l2.cost_alert_usd,
        latency_alert_seconds=config.l2.latency_alert_seconds,
    )
    trigger_manager = TriggerManager(
        task_queue=task_queue,
        cooldown_seconds=config.l2.cooldown_seconds,
        cron_interval_seconds=config.l2.cron_interval_hours * 3600.0,
        thresholds=thresholds,
    )

    # L2TriggerMiddleware（L1 graph after_model，不调 LLM，不改 state）
    l2_trigger_mw = L2TriggerMiddleware(
        trigger_manager=trigger_manager,
        metrics_view=metrics_view,
    )

    # BudgetGuard（三维度，per-day UTC 0 重置）
    budget_limits = _build_budget_limits(config.budget)
    budget_guard = BudgetGuard(
        db_path=config.metrics_db_path,
        limits=budget_limits,
        warning_threshold=config.budget.warning_threshold,
    )

    # OrchestrationMetricsL2（11 种事件类型）
    metrics_l2 = OrchestrationMetricsL2(db_path=config.metrics_db_path)

    # L2EvolutionWorker（daemon 线程 + queue + 6h cron 兜底）
    worker = L2EvolutionWorker(
        task_queue=task_queue,
        metrics_view=metrics_view,
        failure_focuser=focuser,
        evolution_mutator=mutator,
        promotion_gate=gate,
        version_dag=version_dag,
        metrics_l2=metrics_l2,
        cron_interval_seconds=config.l2.cron_interval_hours * 3600.0,
    )

    # L3 eval 装配（config.l3.enabled=false → None，L2 行为不变）
    l3_setup: Any = None
    if config.l3.enabled:
        from poirot.backend.agents.multiagent.eval.bootstrap import setup_l3
        l3_setup = setup_l3(config, metrics_store, task_queue)

    return L2Setup(
        version_dag=version_dag,
        promotion_gate=gate,
        evolution_mutator=mutator,
        trigger_manager=trigger_manager,
        l2_trigger_middleware=l2_trigger_mw,
        budget_guard=budget_guard,
        metrics_l2=metrics_l2,
        worker=worker,
        task_queue=task_queue,
        l3_setup=l3_setup,
    )


def _build_budget_limits(budget_config: BudgetConfig) -> dict[str, BudgetLimit]:
    """从 BudgetConfig 构造 per-specialist BudgetLimit dict。

    对 codex / claude / subagent / pi 四个 specialist，把配置字段
    （per_day_tokens / per_day_cost_usd / per_day_calls）转成 BudgetLimit。
    """
    limits: dict[str, BudgetLimit] = {}
    for name in ("codex", "claude", "subagent", "pi"):
        attr = getattr(budget_config, name, None)
        if attr is not None:
            limits[name] = BudgetLimit(
                per_day_tokens=attr.per_day_tokens,
                per_day_cost_usd=attr.per_day_cost_usd,
                per_day_calls=attr.per_day_calls,
            )
    return limits