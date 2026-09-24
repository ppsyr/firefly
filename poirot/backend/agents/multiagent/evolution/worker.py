"""L2EvolutionWorker — daemon-thread cron worker + per-profile 串行。

【整体职责】
L2 演化的消费端：启动一个 daemon 线程，从共享队列消费 EvolutionTask，
按固定编排（Focuser → Mutator → Gate → VersionDAG）执行演化，并打点 metrics。
失败时 catch + log + continue，不阻塞；同一 pattern 连续失败 3 次则标记 blocked。

【内容摘要】
- _BLOCKED_AUTO_RELEASE_SECONDS / _CONSECUTIVE_FAILURE_THRESHOLD : 阻断与失败阈值常量。
- L2EvolutionWorker : worker 主类（start / stop / run / _run_evolution / _worker_loop）。
- is_running (property) : 线程是否在运行。

【职责边界】
- 只负责：消费任务、编排演化、打点、失败处理、阻断标记。
- 不负责：触发判定（TriggerManager 负责）、变异/评估/晋升逻辑（各组件负责）、
  任务生产（L2TriggerMiddleware 负责）。
- 不持有重状态：持有 queue / 各组件引用 / stop_event / failure_counts。

【INVARIANT】
- daemon 线程：threading.Thread(daemon=True) + queue.Queue。
- per-profile 串行：单 daemon 线程消费 queue，天然串行。
- 失败处理：catch + log + continue，不阻塞 worker loop。
- 同一 pattern 连续失败 3 次 → record_blocked_marked（evolution_blocked）。
- 阻断 24h 后自动释放（auto_release_at）。
- 6h cron 兜底：worker loop 在 queue 空时等待（切片 sleep 以响应 stop_event）。
- 演化编排固定：Focuser → get_active → Mutator → Gate.evaluate → Gate.decide → VersionDAG.commit。
- dominant_category 为 None（不可演化类主导）→ REJECT，不演化。
- Mutator 失败 → FAILED，保持旧 is_active。
- ACCEPT 和 REJECT 都 commit（reject 也存，防重复尝试）。
- 无活跃模板 → REJECT。
- start 幂等：线程已活着则不重启。
- stop 等待最多 5 秒。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any

from poirot.backend.agents.multiagent.evolution.evolution_mutator import EvolutionMutator
from poirot.backend.agents.multiagent.evolution.failure_focuser import FailureFocuser
from poirot.backend.agents.multiagent.evolution.metrics_l2 import OrchestrationMetricsL2
from poirot.backend.agents.multiagent.evolution.metrics_view import MetricsView
from poirot.backend.agents.multiagent.evolution.types import (
    EvolutionResult,
    EvolutionTask,
    FailureCategory,
    PromotionDecision,
)
from poirot.backend.agents.multiagent.evolution.version_dag import VersionDAG

logger = logging.getLogger(__name__)

# 阻断自动释放时间（24h）。
_BLOCKED_AUTO_RELEASE_SECONDS = 86400
# 连续失败阈值（达到则标记 blocked）。
_CONSECUTIVE_FAILURE_THRESHOLD = 3


class L2EvolutionWorker:
    """daemon-thread cron worker + per-profile 串行。

    - daemon 线程 + queue.Queue（不用 cron 框架）。
    - per-profile 串行：单线程消费 queue。
    - 失败处理：catch + log + continue，不阻塞。
    - 连续 3 次失败 → evolution_blocked。
    - 6h cron 兜底：worker loop 等待。
    """

    def __init__(
        self,
        task_queue: "queue.Queue[EvolutionTask]",
        metrics_view: MetricsView,
        failure_focuser: FailureFocuser,
        evolution_mutator: EvolutionMutator,
        promotion_gate: Any,
        version_dag: VersionDAG,
        metrics_l2: OrchestrationMetricsL2,
        cron_interval_seconds: float = 21600.0,  # 6h
    ) -> None:
        """初始化。

        Args:
            task_queue: 共享任务队列（Middleware enqueue，Worker 消费）。
            metrics_view: L2 MetricsView Protocol（读 L1 指标）。
            failure_focuser: 失败聚焦器。
            evolution_mutator: 变异器。
            promotion_gate: 晋升门（evaluate + decide）。
            version_dag: 版本 DAG（commit + get_active）。
            metrics_l2: L2 指标打点。
            cron_interval_seconds: 周期兜底间隔（秒），默认 6h。
        """
        self._queue = task_queue
        self._metrics_view = metrics_view
        self._focuser = failure_focuser
        self._mutator = evolution_mutator
        self._gate = promotion_gate
        self._version_dag = version_dag
        self._metrics_l2 = metrics_l2
        self._cron_interval = cron_interval_seconds

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._failure_counts: dict[str, int] = {}  # per failure_pattern

    def start(self) -> None:
        """启动 daemon 线程 + worker loop。幂等：线程已活着则不重启。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止 daemon 线程（set stop_event + join 最多 5 秒）。"""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def run(self, task: EvolutionTask) -> EvolutionResult:
        """执行单个演化任务（由 daemon 线程保证 per-profile 串行）。

        编排：TriggerManager → MetricsView → FailureFocuser → EvolutionMutator
        → PromotionGate → VersionDAG commit。

        异常兜底：catch + log + 打点 + 记录连续失败 + 返回 FAILED。

        Args:
            task: 演化任务。

        Returns:
            EvolutionResult。
        """
        self._metrics_l2.record_evolution_start(
            experiment_id=task.task_id,
            artifact_type=task.artifact_type,
            from_version="",
        )

        try:
            result = self._run_evolution(task)
            return result
        except Exception as e:
            logger.exception("L2EvolutionWorker.run failed: %s", e)
            self._metrics_l2.record_evolution_failed(
                failure_type="evolution_error",
                experiment_id=task.task_id,
                detail=str(e),
            )
            self._record_consecutive_failure(task.profile, "evolution_error")
            return EvolutionResult(
                task_id=task.task_id,
                decision=PromotionDecision.FAILED,
                error=str(e),
            )

    def _run_evolution(self, task: EvolutionTask) -> EvolutionResult:
        """演化编排闭环：focus → mutate → evaluate → gate → commit。

        流程：
        1. focuser.analyze → FailureStats。
        2. dominant_category 为 None → REJECT（不可演化）。
        3. 按 artifact_type 取当前活跃模板；无 → REJECT。
        4. mutator.evolve_* → EvolutionResult。
        5. 变异失败 → FAILED（保持旧 is_active）。
        6. 成功后清空该 profile 的失败计数。
        7. gate.evaluate + gate.decide。
        8. ACCEPT / REJECT 都 commit 到 VersionDAG。
        9. 打点 metrics_l2 + 返回 EvolutionResult。
        """
        # 1. focus
        failures = self._focuser.analyze(self._metrics_view, task.profile)

        if failures.dominant_category is None:
            # GOAL_UNCLEAR / SANDBOX_ISSUE dominant → 不演化
            return EvolutionResult(
                task_id=task.task_id,
                decision=PromotionDecision.REJECT,
                rationale=f"non-evolvable dominant: {failures.dominant_category}",
            )

        # 2. 取当前活跃模板
        if task.artifact_type == "skill_injection":
            from poirot.backend.agents.multiagent.evolution.types import SkillInjectionTemplate
            current = self._version_dag.get_active(SkillInjectionTemplate)
            if current is None:
                return EvolutionResult(
                    task_id=task.task_id,
                    decision=PromotionDecision.REJECT,
                    rationale="no active skill_injection template",
                )
            mutate_result = self._mutator.evolve_skill_injection(current, failures)
        else:
            from poirot.backend.agents.multiagent.evolution.types import ContextSummaryTemplate
            current = self._version_dag.get_active(ContextSummaryTemplate)
            if current is None:
                return EvolutionResult(
                    task_id=task.task_id,
                    decision=PromotionDecision.REJECT,
                    rationale="no active context_summary template",
                )
            mutate_result = self._mutator.evolve_context_summary(current, failures)

        # 3. mutator 失败 → 保持旧 is_active
        if not mutate_result.success:
            self._metrics_l2.record_evolution_failed(
                failure_type=mutate_result.failure_type,
                experiment_id=task.task_id,
                detail=mutate_result.rationale,
            )
            self._record_consecutive_failure(task.profile, mutate_result.failure_type)
            return EvolutionResult(
                task_id=task.task_id,
                decision=PromotionDecision.FAILED,
                error=mutate_result.failure_type,
                rationale=mutate_result.rationale,
            )

        # 成功后清空该 profile 的连续失败计数
        prefix = f"{task.profile}:"
        for key in list(self._failure_counts.keys()):
            if key.startswith(prefix):
                self._failure_counts.pop(key, None)

        # 4. promotion gate（hash 防环 + Wilson CI）
        candidate = mutate_result.candidate
        from poirot.backend.agents.multiagent.evolution.promotion_gate import EvalTask
        # MVP：空 task_sample（暂无真实 eval 数据），走 floor eval
        eval_result = self._gate.evaluate(candidate, current, [])
        decision = self._gate.decide(candidate, current, eval_result)

        # 5. commit 到 VersionDAG
        if decision == PromotionDecision.ACCEPT:
            artifact_id = self._version_dag.commit(
                candidate,
                eval_result,
                trigger_source=task.trigger_source.value,
                trigger_detail=task.trigger_detail,
                rationale=mutate_result.rationale,
                decision="accept",
            )
            self._metrics_l2.record_promotion_decision(
                decision="accept",
                candidate_score=eval_result.candidate_score,
                baseline_score=eval_result.baseline_score,
                ci_low=eval_result.ci_low,
                ci_high=eval_result.ci_high,
            )
            return EvolutionResult(
                task_id=task.task_id,
                decision=PromotionDecision.ACCEPT,
                rationale=mutate_result.rationale,
            )
        else:
            # reject → 也 commit 到 DAG（防重复尝试）
            self._version_dag.commit(
                candidate,
                eval_result,
                trigger_source=task.trigger_source.value,
                trigger_detail=task.trigger_detail,
                rationale=mutate_result.rationale,
                decision="reject",
            )
            self._metrics_l2.record_promotion_decision(
                decision="reject",
                candidate_score=eval_result.candidate_score,
                baseline_score=eval_result.baseline_score,
                ci_low=eval_result.ci_low,
                ci_high=eval_result.ci_high,
            )
            return EvolutionResult(
                task_id=task.task_id,
                decision=PromotionDecision.REJECT,
                rationale=mutate_result.rationale,
            )

    def _worker_loop(self) -> None:
        """daemon 线程 worker loop：消费 queue + 6h cron 兜底。

        - queue 有任务 → 立即 run。
        - queue 空 → 等待（切片 sleep 以响应 stop_event）。
        - run 异常 → catch + log（不崩线程）。
        """
        while not self._stop_event.is_set():
            try:
                task = self._queue.get(timeout=1.0)
            except queue.Empty:
                # 6h cron 兜底（切片 sleep 以允许 stop_event 检查）
                self._stop_event.wait(timeout=min(self._cron_interval, 60.0))
                continue

            try:
                self.run(task)
            except Exception as e:
                logger.exception("L2EvolutionWorker worker_loop task failed: %s", e)

    def _record_consecutive_failure(self, profile: str, failure_type: str) -> None:
        """记录连续失败；同一 pattern 达阈值则标记 blocked。

        key = f"{profile}:{failure_type}"。
        达 _CONSECUTIVE_FAILURE_THRESHOLD → record_blocked_marked + 计数归零
        （24h 后由 auto_release_at 自动释放）。
        """
        key = f"{profile}:{failure_type}"
        self._failure_counts[key] = self._failure_counts.get(key, 0) + 1
        if self._failure_counts[key] >= _CONSECUTIVE_FAILURE_THRESHOLD:
            auto_release = time.time() + _BLOCKED_AUTO_RELEASE_SECONDS
            self._metrics_l2.record_blocked_marked(
                blocked_pattern=key,
                blocked_type="evolution",
                auto_release_at=str(int(auto_release)),
            )
            # 标记后归零（24h 自动释放）
            self._failure_counts[key] = 0

    @property
    def is_running(self) -> bool:
        """线程是否在运行。"""
        return self._thread is not None and self._thread.is_alive()