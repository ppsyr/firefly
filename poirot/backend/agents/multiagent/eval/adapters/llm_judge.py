"""L3 LLMJudgeAdapter — LLM 四维加权评分评估器。

【整体职责】
实现 EvalAdapter 契约：用 LLM 对 candidate / baseline 在任务样本上打分，
四维加权汇总为 [0,1] 分数，再算 Wilson 95% 置信区间，返回 EvalResult。
评分函数 judge_fn 由 bootstrap 注入。

【内容摘要】
- JudgeFn                     : 评分函数类型别名（artifact + task → float [0,1]）。
- LLMJudgeAdapter             : 评估器主类，实现 evaluate + health_check。
- WEIGHTS                     : 四维权重常量（task_completion / response_quality /
                                efficiency / tool_usage）。

【职责边界】
- 只负责：遍历 task_sample 调 judge_fn、聚合分数、算 Wilson CI、返回 EvalResult。
- 不负责：judge_fn 的实现（bootstrap 注入）、评估方法选择（Bridge 负责）、
  结果持久化（db 负责）。
- 不持有运行时状态：judge_fn / model / z 由构造注入。

【INVARIANT】
- 实现 EvalAdapter Protocol（evaluate + health_check）——无需显式继承。
- WEIGHTS 与 skill TaskQualityJudge 一致（0.50 / 0.35 / 0.05 / 0.10），
  但不实现 skill Protocol——skill 是 async，L3 是 sync。
- judge_fn 为 None → evaluate 返回 success=False + failure_reason="no judge_fn configured"。
- 单任务异常 → 跳过（continue）；全 task 失败 → success=False + "all_tasks_failed"。
- 分数裁剪到 [0.0, 1.0]（max/min 双向 clamp）。
- 置信区间用 Wilson CI（_wilson_ci），z_score 默认 1.96（95%）。
- llm_judge_model 默认 None：继承 lead 模型（与 L2 EvolutionMutator 同款）。
- health_check：judge_fn 非 None 即可用。
- 不复用 skill 的 TaskQualityJudge：skill 是 async，L3 是 sync。
"""
from __future__ import annotations

from typing import Any, Callable

from poirot.backend.agents.multiagent.eval.types import EvalContext
from poirot.backend.agents.multiagent.evolution.promotion_gate import (
    EvalResult,
    EvalTask,
    EvolutionArtifact,
    _wilson_ci,
)

# 评分函数类型：接收 artifact 与 task，返回 [0,1] 分数。
JudgeFn = Callable[[EvolutionArtifact, EvalTask], float]


class LLMJudgeAdapter:
    """LLM-judge 评估器——四维加权评分 + Wilson 95% CI。

    实现 EvalAdapter Protocol（evaluate + health_check）。
    WEIGHTS 与 skill TaskQualityJudge 一致，但不实现 skill Protocol
    （skill 是 async，L3 是 sync）。
    judge_fn 由 bootstrap 注入（MVP 可为 None，数据驱动触发后才装配）。
    """

    # 四维权重：任务完成 / 响应质量 / 效率 / 工具使用。
    WEIGHTS = {
        "task_completion": 0.50,
        "response_quality": 0.35,
        "efficiency": 0.05,
        "tool_usage": 0.10,
    }

    def __init__(
        self,
        judge_fn: JudgeFn | None = None,
        llm_judge_model: str | None = None,
        z_score: float = 1.96,
    ) -> None:
        """初始化。

        Args:
            judge_fn: 评分函数；为 None 时 evaluate 直接返回失败。
            llm_judge_model: LLM 评判所用模型名；None 表示继承 lead 模型。
            z_score: Wilson CI 的 z 值，默认 1.96（95% 置信度）。
        """
        self._judge_fn = judge_fn
        self._llm_judge_model = llm_judge_model
        self._z = z_score

    def evaluate(self, ctx: EvalContext) -> EvalResult:
        """对 task_sample 逐个评分 candidate / baseline，返回聚合 EvalResult。

        流程：
        1. judge_fn 为 None → 返回失败（no judge_fn configured）。
        2. 遍历 task_sample，对 candidate / baseline 各调一次 judge_fn，分数 clamp 到 [0,1]。
        3. 单任务异常 → 跳过；全部失败 → 返回失败（all_tasks_failed）。
        4. 计算平均分 + Wilson CI。
        5. 返回 EvalResult（success=True）。
        """
        if self._judge_fn is None:
            return EvalResult(
                candidate_score=0.0, baseline_score=0.0,
                ci_low=0.0, ci_high=0.0, sample_size=0,
                method_used="llm_judge",
                success=False, failure_reason="no judge_fn configured",
            )

        candidate_scores: list[float] = []
        baseline_scores: list[float] = []

        for task in ctx.task_sample:
            try:
                c_score = self._judge_fn(ctx.candidate, task)
                b_score = self._judge_fn(ctx.baseline, task)
            except Exception:
                continue
            candidate_scores.append(max(0.0, min(1.0, c_score)))
            baseline_scores.append(max(0.0, min(1.0, b_score)))

        if not candidate_scores:
            return EvalResult(
                candidate_score=0.0, baseline_score=0.0,
                ci_low=0.0, ci_high=0.0, sample_size=0,
                method_used="llm_judge",
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
            method_used="llm_judge",
            success=True,
        )

    def health_check(self) -> bool:
        """健康检查：judge_fn 已配置即可用。"""
        return self._judge_fn is not None