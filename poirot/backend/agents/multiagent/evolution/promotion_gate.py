"""PromotionGate — longitudinal pairs eval + Wilson 95% CI + hash 防环。

【整体职责】
L2 的晋升门：先评估 candidate vs baseline（longitudinal pairs，或经 L3 bridge 分发），
再根据评估结果与 hash 防环规则决定 ACCEPT / REJECT / FAILED。
是"变异 → 评估 → 晋升"链条的最后一环。

【内容摘要】
- EvalTask(frozen)          : 单个评估任务（一条历史 specialist 调用记录）。
- EvalResult(frozen)        : 评估结果（candidate / baseline 分数 + CI + 元信息）。
- Evaluator(Protocol)       : 评估器抽象（evaluate(artifact, task) -> bool）。
- PromotionGate             : 晋升门主类（evaluate / decide）。
- _wilson_ci()              : Wilson score interval 计算（小样本友好）。

【职责边界】
- 只负责：评估 candidate vs baseline、按 hash + CI 规则决策。
- 不负责：变异（mutator 负责）、版本持久化（version_dag 负责）、
  L3 内部评估实现（bridge / adapter 负责）。
- 不持有重状态：只持有 evaluator / version_dag / bridge / _task_use_count。

【INVARIANT】
- hash 命中近 5 版 → REJECT（防环）。
- candidate CI 下界 > baseline CI 上界 → ACCEPT。
- eval 失败 → FAILED（保持旧 is_active）。
- Wilson score interval：z=1.96，小样本友好，p=0/1 不退化。
- eval 整体超时（默认 30min）→ 返回 success=False + "overall_timeout"。
- task 累计使用次数 ≤ eval_task_max_reuse（默认 3，防过拟合）。
- 单 task 异常 → 跳过（continue）。
- bridge 非 None 时优先走 L3（构造 EvalContext 调 bridge.evaluate）。
- evaluator 为 None 且 bridge 为 None → 返回失败（no evaluator configured）。
- EvalResult 只存 candidate 的 CI；baseline CI 在 decide 里重算。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from poirot.backend.agents.multiagent.evolution.types import (
    EvolutionArtifact,
    PromotionDecision,
)

if TYPE_CHECKING:
    from poirot.backend.agents.multiagent.eval.bridge import EvalBridge
    from poirot.backend.agents.multiagent.eval.types import EvalContext


@dataclass(frozen=True)
class EvalTask:
    """单个评估任务——一条历史 specialist 调用记录（L2 演化时抽样传入）。

    字段：
    - task_id: 任务唯一 ID（用于累计使用次数）。
    - goal / success_criteria: 任务定义。
    - sandbox_id: 沙箱 ID（可选）。
    - context_snapshot_ref: 上下文快照引用（可选）。
    - expected_outcome: 期望结果（可选）。
    """

    task_id: str
    goal: str
    success_criteria: str
    sandbox_id: str | None = None
    context_snapshot_ref: str | None = None
    expected_outcome: str | None = None


@dataclass(frozen=True)
class EvalResult:
    """评估结果（L2 自建同构，不 import skill）。

    与 skill EvalResult 同构但独立——skill 含 SkillRecord，L2 含 EvolutionArtifact。

    字段：
    - candidate_score / baseline_score: 两者的平均分。
    - ci_low / ci_high: candidate 的 Wilson CI（只存 candidate）。
    - sample_size: 样本数。
    - method_used: 评估方法名。
    - success: 评估是否成功。
    - failure_reason: 失败原因（成功时为 None）。
    """

    candidate_score: float
    baseline_score: float
    ci_low: float
    ci_high: float
    sample_size: int
    method_used: str
    raw_data_ref: str | None = None
    success: bool = True
    failure_reason: str | None = None


class Evaluator(Protocol):
    """评估器抽象。

    evaluate(artifact, task) -> bool：跑 artifact 在 task 上，返回 success_criteria_met。
    """

    def evaluate(self, artifact: EvolutionArtifact, task: EvalTask) -> bool: ...


class PromotionGate:
    """longitudinal pairs eval + Wilson 95% CI + hash 防环。

    - hash 命中近 5 版 → REJECT（防环）。
    - candidate CI 下界 > baseline CI 上界 → ACCEPT。
    - eval 失败 → FAILED。
    - Wilson CI（z=1.96，小样本友好）。
    - eval 超时（默认 30min）→ 中断 + 保持旧 is_active。
    - task 累计 ≤ 3 次（防过拟合）。
    - bridge 非 None 时优先走 L3。
    """

    def __init__(
        self,
        evaluator: Evaluator | None = None,
        version_dag: Any | None = None,
        eval_timeout_seconds: float = 1800.0,  # 30min
        eval_sample_min: int = 10,
        eval_sample_max: int = 15,
        eval_task_max_reuse: int = 3,
        z_score: float = 1.96,
        bridge: "EvalBridge | None" = None,
    ) -> None:
        """初始化。

        Args:
            evaluator: 评估器（MVP 可为 None）；bridge 为 None 时用它做 floor eval。
            version_dag: 版本 DAG，提供 hash_exists_in_recent 用于防环。
            eval_timeout_seconds: 评估整体超时（秒），默认 30min。
            eval_sample_min / eval_sample_max: 样本数范围（MVP 未使用，预留）。
            eval_task_max_reuse: 单 task 累计最大使用次数（防过拟合），默认 3。
            z_score: Wilson CI 的 z 值，默认 1.96。
            bridge: L3 EvalBridge；非 None 时优先走 L3 评估。
        """
        self._evaluator = evaluator
        self._version_dag = version_dag
        self._eval_timeout = eval_timeout_seconds
        self._sample_min = eval_sample_min
        self._sample_max = eval_sample_max
        self._task_max_reuse = eval_task_max_reuse
        self._z = z_score
        self._bridge = bridge
        # task 累计使用次数（防过拟合）
        self._task_use_count: dict[str, int] = {}

    def evaluate(
        self,
        candidate: EvolutionArtifact,
        baseline: EvolutionArtifact,
        task_sample: list[EvalTask],
    ) -> EvalResult:
        """longitudinal pairs eval：candidate vs baseline 各跑 task_sample。

        流程：
        1. bridge 非 None → 构造 EvalContext，调 bridge.evaluate（L3 分发）。
        2. evaluator 为 None → 返回失败（no evaluator configured）。
        3. 过滤累计超 _task_max_reuse 的 task（防过拟合）。
        4. 遍历 task，各评估 candidate / baseline；单 task 异常跳过。
        5. 每次执行前检查超时。
        6. 全部失败 → 返回失败（all_tasks_failed）。
        7. 算平均分 + Wilson CI，返回 EvalResult。

        Args:
            candidate: L2 变异出的新版本。
            baseline: 当前活跃版本。
            task_sample: 抽样出的评估任务列表。

        Returns:
            EvalResult（success / candidate_score / baseline_score / CI）。
        """
        # L3 bridge 分发：bridge 非 None 时调 bridge.evaluate(ctx)，否则既有 floor eval
        if self._bridge is not None:
            from poirot.backend.agents.multiagent.eval.types import EvalContext
            ctx = EvalContext(
                candidate=candidate,
                baseline=baseline,
                task_sample=tuple(task_sample),
            )
            return self._bridge.evaluate(ctx)

        if self._evaluator is None:
            return EvalResult(
                candidate_score=0.0, baseline_score=0.0,
                ci_low=0.0, ci_high=0.0, sample_size=0,
                method_used="programmatic_floor",
                success=False, failure_reason="no evaluator configured",
            )

        # 过滤累计超 max_reuse 的 task
        filtered = [t for t in task_sample if self._task_use_count.get(t.task_id, 0) < self._task_max_reuse]
        if not filtered:
            return EvalResult(
                candidate_score=0.0, baseline_score=0.0,
                ci_low=0.0, ci_high=0.0, sample_size=0,
                method_used="programmatic_floor",
                success=False, failure_reason="insufficient_tasks",
            )

        start = time.time()
        candidate_scores: list[float] = []
        baseline_scores: list[float] = []

        for task in filtered:
            # 超时检查（执行前）
            if time.time() - start > self._eval_timeout:
                return EvalResult(
                    candidate_score=0.0, baseline_score=0.0,
                    ci_low=0.0, ci_high=0.0, sample_size=len(candidate_scores),
                    method_used="programmatic_floor",
                    success=False, failure_reason="overall_timeout",
                )
            # 累计使用次数 +1
            self._task_use_count[task.task_id] = self._task_use_count.get(task.task_id, 0) + 1
            try:
                c_met = self._evaluator.evaluate(candidate, task)
                b_met = self._evaluator.evaluate(baseline, task)
            except Exception:
                # 单 task 跑挂 skip
                continue
            candidate_scores.append(1.0 if c_met else 0.0)
            baseline_scores.append(1.0 if b_met else 0.0)

        if not candidate_scores:
            return EvalResult(
                candidate_score=0.0, baseline_score=0.0,
                ci_low=0.0, ci_high=0.0, sample_size=0,
                method_used="programmatic_floor",
                success=False, failure_reason="all_tasks_failed",
            )

        candidate_score = sum(candidate_scores) / len(candidate_scores)
        baseline_score = sum(baseline_scores) / len(baseline_scores)
        c_low, c_high = _wilson_ci(candidate_score, len(candidate_scores), self._z)
        b_low, b_high = _wilson_ci(baseline_score, len(baseline_scores), self._z)

        return EvalResult(
            candidate_score=candidate_score,
            baseline_score=baseline_score,
            ci_low=c_low,
            ci_high=c_high,
            sample_size=len(candidate_scores),
            method_used="programmatic_floor",
            success=True,
        )

    def decide(
        self,
        candidate: EvolutionArtifact,
        baseline: EvolutionArtifact,
        eval_result: EvalResult,
    ) -> PromotionDecision:
        """晋升决策。

        规则（顺序）：
        1. hash 命中近 5 版 → REJECT（防环）。
        2. eval 失败 → FAILED（保持旧 is_active）。
        3. candidate CI 下界 > baseline CI 上界 → ACCEPT。
        4. 否则 → REJECT。

        Args:
            candidate: 新版本产物。
            baseline: 当前活跃版本。
            eval_result: evaluate 产出的评估结果。

        Returns:
            PromotionDecision（ACCEPT / REJECT / FAILED）。
        """
        # hash 防环：candidate hash 命中近 5 版 → REJECT
        if self._version_dag is not None:
            if self._version_dag.hash_exists_in_recent(candidate.artifact_hash, window=5):
                return PromotionDecision.REJECT

        # eval 失败 → REJECT（保持旧 is_active）
        if not eval_result.success:
            return PromotionDecision.FAILED

        # baseline CI 上界（用 candidate 的 baseline_score 算，因 EvalResult 只存 candidate CI）
        # 重新算 baseline CI（样本数同 candidate）
        b_low, b_high = _wilson_ci(
            eval_result.baseline_score, eval_result.sample_size, self._z
        )

        # candidate CI 下界 > baseline CI 上界 → ACCEPT
        if eval_result.ci_low > b_high:
            return PromotionDecision.ACCEPT
        return PromotionDecision.REJECT


def _wilson_ci(
    score: float, sample_size: int, z: float = 1.96
) -> tuple[float, float]:
    """Wilson score interval 计算（小样本友好，p=0/1 不退化）。

    公式：p̂ ± z·√(p̂(1-p̂)/n + z²/(4n²)) / (1+z²/n)
    z=1.96 对应 95% CI。

    Args:
        score: 观测比例（会被 clamp 到 [0,1]）。
        sample_size: 样本数；0 时返回 (0.0, 0.0)。
        z: z 值，默认 1.96。

    Returns:
        (low, high) 置信区间，均 clamp 到 [0,1]。
    """
    if sample_size == 0:
        return 0.0, 0.0
    n = sample_size
    p_hat = max(0.0, min(1.0, score))
    denom = 1.0 + z * z / n
    center = (p_hat + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p_hat * (1 - p_hat) / n + z * z / (4 * n * n)) / denom
    low = max(0.0, center - margin)
    high = min(1.0, center + margin)
    return low, high