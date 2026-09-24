"""L3 MultiagentProgrammaticFacade — L3 未启用时 L2 用的 facade。

【整体职责】
实现 EvalBridge 契约：当 L3 评估层未启用时，为 L2 PromotionGate 提供一个"退化的"
评估门面——只用 programmatic 方法（调 L1 ResultSummarizer 的 success_criteria_met），
接口与 L3 启用时的 OrchestrationBridge 完全一致，保证 L2 调用方式不变。

【内容摘要】
- MultiagentProgrammaticFacade : facade 主类，实现 evaluate / list_available_methods / health_check。

【职责边界】
- 只负责：委托 evaluator 做 programmatic 评估，返回 EvalResult。
- 不负责：LLM 评判（llm_judge adapter 负责）、纵向配对（longitudinal_pairs 负责）、
  评估器管理（registry 负责）。
- 不持有运行时状态：evaluator 由构造注入，可为 None。

【INVARIANT】
- 接口与 OrchestrationBridge 一致：L2 PromotionGate 的 bridge 参数类型为 EvalBridge
  Protocol，两个 facade 都满足。
- 复用 L2 的 EvalResult / Evaluator / _wilson_ci：不重复定义。
- list_available_methods 固定返回 ('programmatic',)：facade 只支持这一种方法。
- evaluator 为 None → evaluate 返回 success=False + failure_reason="no evaluator configured"。
- 任务全部失败 → 返回 success=False + failure_reason="all_tasks_failed"。
- 单个任务评估异常 → 跳过（continue），不中断整体评估。
- 置信区间用 Wilson CI（_wilson_ci），z_score 默认 1.96（95%）。
- 无状态、可重复调用。
"""
from __future__ import annotations

from poirot.backend.agents.multiagent.eval.types import EvalContext
from poirot.backend.agents.multiagent.evolution.promotion_gate import (
    EvalResult,
    Evaluator,
    _wilson_ci,
)


class MultiagentProgrammaticFacade:
    """L3 未启用时 L2 用的 facade，实现 EvalBridge 契约。

    内部调 L1 ResultSummarizer.success_criteria_met（通过 evaluator callable 注入）。
    接口与 OrchestrationBridge 一致——L2 无论 L3 是否启用，调用方式统一。
    L3 未启用时 L2 用此 facade（floor eval），L3 启用时换 OrchestrationBridge。
    """

    def __init__(
        self,
        evaluator: Evaluator | None = None,
        z_score: float = 1.96,
    ) -> None:
        """初始化。

        Args:
            evaluator: 评估单个任务的 callable；为 None 时 evaluate 直接返回失败。
            z_score: Wilson CI 的 z 值，默认 1.96（95% 置信度）。
        """
        self._evaluator = evaluator
        self._z = z_score

    def evaluate(self, ctx: EvalContext) -> EvalResult:
        """对 ctx.task_sample 逐个评估 candidate / baseline，返回聚合 EvalResult。

        流程：
        1. evaluator 为 None → 返回失败（no evaluator configured）。
        2. 遍历 task_sample，对 candidate / baseline 各调一次 evaluator。
        3. 单任务异常 → 跳过；全部失败 → 返回失败（all_tasks_failed）。
        4. 计算 candidate / baseline 平均分 + Wilson CI。
        5. 返回 EvalResult（success=True）。
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

    def list_available_methods(self) -> tuple[str, ...]:
        """返回支持的评估方法：只有 programmatic。

        facade 不支持 llm_judge / longitudinal_pairs——那些属于 L3 启用后的
        OrchestrationBridge 能力。
        """
        return ("programmatic",)

    def health_check(self) -> bool:
        """健康检查：evaluator 已配置即可用。"""
        return self._evaluator is not None