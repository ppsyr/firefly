"""L3 ProgrammaticAdapter — programmatic eval（success_criteria_met + Wilson CI）。

【整体职责】
实现 EvalAdapter 契约：让 candidate 与 baseline 在同一批 task_sample 上各跑一遍，
用 evaluator 判定每个 task 的 success_criteria_met（bool），再算 Wilson 95% 置信区间，
返回 EvalResult。是 L3 里最基础的评估方法。

【内容摘要】
- ProgrammaticAdapter : 评估器主类，实现 evaluate + health_check。

【职责边界】
- 只负责：遍历 task_sample 调 evaluator、聚合分数、算 Wilson CI、返回 EvalResult。
- 不负责：task_sample 抽样（L2 负责）、晋升决策（L2 PromotionGate 负责）、
  evaluator 实现（bootstrap 注入）、LLM 评分（llm_judge adapter 负责）。
- 不持有运行时状态：evaluator / z 由构造注入。

【INVARIANT】
- 实现 EvalAdapter Protocol（evaluate + health_check）——无需显式继承。
- method_used 固定为 "programmatic"。
- evaluator 为 None → evaluate 返回 success=False + failure_reason="no evaluator configured"。
- 单任务异常 → 跳过（continue）；全 task 失败 → success=False + "all_tasks_failed"。
- 置信区间用 Wilson CI（_wilson_ci），z_score 默认 1.96（95%）——小样本友好。
- 复用 L2 Evaluator Protocol（evaluate(artifact, task) -> bool）。
- 简单规则检查（输出格式 / artifact 完整性）由 evaluator 内含，不在本类实现。
- health_check：evaluator 非 None 即可用。
- MVP evaluator=None：floor eval 由 L2 PromotionGate 直接调 facade；本 adapter 数据驱动触发后才装配。
"""
from __future__ import annotations

from typing import Callable

from poirot.backend.agents.multiagent.eval.types import EvalContext
from poirot.backend.agents.multiagent.evolution.promotion_gate import (
    EvalResult,
    Evaluator,
    _wilson_ci,
)


class ProgrammaticAdapter:
    """programmatic 评估器——success_criteria_met + Wilson 95% CI。

    实现 EvalAdapter Protocol（evaluate + health_check）。
    evaluator 由 bootstrap 注入（MVP 可为 None，数据驱动触发后才装配）。
    """

    def __init__(
        self,
        evaluator: Evaluator | None = None,
        z_score: float = 1.96,
    ) -> None:
        """初始化。

        Args:
            evaluator: 单个任务的评估器（evaluate(artifact, task) -> bool）；
                为 None 时 evaluate 直接返回失败。
            z_score: Wilson CI 的 z 值，默认 1.96（95% 置信度）。
        """
        self._evaluator = evaluator
        self._z = z_score

    def evaluate(self, ctx: EvalContext) -> EvalResult:
        """对 task_sample 逐个评估 candidate / baseline，返回聚合 EvalResult。

        流程：
        1. evaluator 为 None → 返回失败（no evaluator configured）。
        2. 遍历 task_sample，对 candidate / baseline 各调一次 evaluator。
        3. 单任务异常 → 跳过；全部失败 → 返回失败（all_tasks_failed）。
        4. 计算 candidate / baseline 平均分 + Wilson CI。
        5. 返回 EvalResult（success=True, method_used="programmatic"）。
        """
        if self._evaluator is None:
            return EvalResult(
                candidate_score=0.0, baseline_score=0.0,
                ci_low=0.0, ci_high=0.0, sample_size=0,
                method_used="programmatic",
                success=False, failure_reason="no evaluator configured",
            )

        candidate_scores: list[float] = []
        baseline_scores: list[float] = []

        for task in ctx.task_sample:
            try:
                c_met = self._evaluator.evaluate(ctx.candidate, task)
                b_met = self._evaluator.evaluate(ctx.baseline, task)
            except Exception:
                continue
            candidate_scores.append(1.0 if c_met else 0.0)
            baseline_scores.append(1.0 if b_met else 0.0)

        if not candidate_scores:
            return EvalResult(
                candidate_score=0.0, baseline_score=0.0,
                ci_low=0.0, ci_high=0.0, sample_size=0,
                method_used="programmatic",
                success=False, failure_reason="all_tasks_failed",
            )

        c_score = sum(candidate_scores) / len(candidate_scores)
        b_score = sum(baseline_scores) / len(baseline_scores)
        c_low, c_high = _wilson_ci(c_score, len(candidate_scores), self._z)

        return EvalResult(
            candidate_score=c_score,
            baseline_score=b_score,
            ci_low=c_low,
            ci_high=c_high,
            sample_size=len(candidate_scores),
            method_used="programmatic",
            success=True,
        )

    def health_check(self) -> bool:
        """健康检查：evaluator 已配置即可用。"""
        return self._evaluator is not None