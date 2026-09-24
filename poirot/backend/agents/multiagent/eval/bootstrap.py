"""L3 eval bootstrap — 装配 L3 组件 + 启动健康检查 daemon 线程。

【整体职责】
L3 评估层的装配入口：当 config.l3.enabled=true 时，构造 L3 的全部组件
（registry + 3 adapter + bridge + runtime_tracker + decision_log writer/reader + facade），
并启动一个 6 小时周期的健康检查 daemon 线程；enabled=false 时返回 None，L2 行为不变。

【内容摘要】
- L3Setup(dataclass)             : L3 装配结果容器（注入 L2Setup 供 L1 bootstrap 使用）。
- setup_l3(config, metrics_store, task_queue) : 装配主入口。
- _start_health_check_thread()   : 启动 6h 周期的健康检查 daemon 线程。

【职责边界】
- 只负责：构造 L3 组件、启动健康检查线程、打包 L3Setup。
- 不负责：L3 评估逻辑（adapter 负责）、健康检查逻辑（runtime_tracker 负责）、
  L2 进化触发（只 enqueue，由 L2 消费）。
- 不持有状态：装配产出 L3Setup 后返回。

【INVARIANT】
- enabled=false → 返回 None，L2 行为完全不变。
- 3 个 adapter 在 MVP 阶段 evaluator / judge_fn 均为 None（数据驱动触发后才装配）。
- registry.register 按固定顺序：programmatic → llm_judge → longitudinal_pairs。
- OrchestrationBridge 接收 registry，作为 L2 PromotionGate 的 bridge。
- SpecialistRuntimeTracker 接收 metrics_store + degradation_delta。
- DecisionLogWriter/Reader 共用同一个 metrics_store（L1 MultiAgentMetricsStore）。
- MultiagentProgrammaticFacade 即使 L3 启用也构造（向后兼容备用）。
- 健康检查线程是 daemon：主进程退出即结束，不阻塞退出。
- 健康检查周期 6 小时；异常被吞掉（daemon 不崩溃）。
- 健康检查只 enqueue L2 cron queue，不直接调 L2 TriggerManager。
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Any

from poirot.backend.agents.multiagent.config import MultiAgentConfig
from poirot.backend.agents.multiagent.eval.adapters.llm_judge import LLMJudgeAdapter
from poirot.backend.agents.multiagent.eval.adapters.longitudinal_pairs import (
    LongitudinalPairsAdapter,
)
from poirot.backend.agents.multiagent.eval.adapters.programmatic import (
    ProgrammaticAdapter,
)
from poirot.backend.agents.multiagent.eval.bridge import OrchestrationBridge
from poirot.backend.agents.multiagent.eval.decision_log import (
    DecisionLogReader,
    DecisionLogWriter,
)
from poirot.backend.agents.multiagent.eval.facade import MultiagentProgrammaticFacade
from poirot.backend.agents.multiagent.eval.registry import SpecialistEvalRegistry
from poirot.backend.agents.multiagent.eval.runtime_tracker import (
    SpecialistRuntimeTracker,
    trigger_l2_evolution_if_degraded,
)


@dataclass
class L3Setup:
    """L3 装配结果——注入 L2Setup 供 L1 bootstrap 使用。

    持有 L3 的全部运行时组件；health_thread 是后台健康检查线程。
    """

    bridge: OrchestrationBridge
    registry: SpecialistEvalRegistry
    runtime_tracker: SpecialistRuntimeTracker
    decision_log_writer: DecisionLogWriter
    decision_log_reader: DecisionLogReader
    facade: MultiagentProgrammaticFacade
    health_thread: threading.Thread


def setup_l3(
    config: MultiAgentConfig,
    metrics_store: Any,
    task_queue: queue.Queue,
) -> L3Setup | None:
    """装配 L3 评估层。

    - config.l3.enabled=false → 返回 None（L2 行为不变）。
    - config.l3.enabled=true → 构造全部 L3 组件 + 启动 daemon 线程。

    装配顺序：
    1. SpecialistEvalRegistry + 注册 3 个 adapter（MVP 均为 None）。
    2. OrchestrationBridge（接收 registry）。
    3. SpecialistRuntimeTracker（接收 metrics_store + degradation_delta）。
    4. DecisionLogWriter / Reader（共用 metrics_store）。
    5. MultiagentProgrammaticFacade（向后兼容备用）。
    6. 启动健康检查 daemon 线程（6h 周期）。
    7. 打包 L3Setup。
    """
    if not config.l3.enabled:
        return None

    # SpecialistEvalRegistry + 3 adapter（MVP evaluator=None，数据驱动触发后才装配）
    registry = SpecialistEvalRegistry()
    registry.register("programmatic", ProgrammaticAdapter(evaluator=None))
    registry.register("llm_judge", LLMJudgeAdapter(judge_fn=None))
    registry.register("longitudinal_pairs", LongitudinalPairsAdapter(evaluator=None))

    # OrchestrationBridge（作为 L2 PromotionGate 的 bridge 注入）
    bridge = OrchestrationBridge(adapter_registry=registry)

    # SpecialistRuntimeTracker（健康监控 + degraded 检测）
    runtime_tracker = SpecialistRuntimeTracker(
        metrics_view=metrics_store,
        degradation_delta=config.l3.degradation_delta,
    )

    # DecisionLog Writer/Reader（跨 run lessons 累积）
    decision_log_writer = DecisionLogWriter(store=metrics_store)
    decision_log_reader = DecisionLogReader(store=metrics_store)

    # MultiagentProgrammaticFacade（L3 未启用时备用，向后兼容）
    facade = MultiagentProgrammaticFacade(evaluator=None)

    # 启动 L3 健康监控 daemon 线程（6h 周期，复用 L2 daemon thread pattern）
    health_thread = _start_health_check_thread(
        runtime_tracker=runtime_tracker,
        task_queue=task_queue,
        threshold=config.l3.degradation_threshold,
        interval_seconds=6 * 3600.0,
    )

    return L3Setup(
        bridge=bridge,
        registry=registry,
        runtime_tracker=runtime_tracker,
        decision_log_writer=decision_log_writer,
        decision_log_reader=decision_log_reader,
        facade=facade,
        health_thread=health_thread,
    )


def _start_health_check_thread(
    runtime_tracker: SpecialistRuntimeTracker,
    task_queue: queue.Queue,
    threshold: float,
    interval_seconds: float,
) -> threading.Thread:
    """启动 L3 健康监控 daemon 线程（6h 周期）。

    循环：调 trigger_l2_evolution_if_degraded → 等待 interval。
    该函数只 enqueue L2 cron queue，不直接调 L2 TriggerManager。
    异常被吞掉——daemon 线程不崩溃。

    Args:
        runtime_tracker: 健康追踪器，提供 degraded 检测。
        task_queue: L2 cron 队列，用于 enqueue 进化触发。
        threshold: 退化阈值。
        interval_seconds: 检查周期（秒），调用方传 6*3600。

    Returns:
        已启动的 daemon 线程对象。
    """
    stop_event = threading.Event()

    def _health_loop() -> None:
        while not stop_event.is_set():
            try:
                trigger_l2_evolution_if_degraded(
                    tracker=runtime_tracker,
                    cron_queue=task_queue,
                    threshold=threshold,
                )
            except Exception:
                pass  # daemon 线程不崩溃
            stop_event.wait(timeout=interval_seconds)

    thread = threading.Thread(target=_health_loop, daemon=True, name="l3-health-check")
    thread.start()
    return thread