"""ScoreDeltaGate — 基础门（零 LLM，用 EvalResult）。

【整体职责】
evolution 闭环的门控环节：用 EvalResult 决定 candidate 能否晋升。
规则：candidate.score > baseline.score 且无 hard_failure → accept；否则 reject。
- 零 LLM：只看 EvalResult 的分数与 hard_failures。
- CAPTURED 特例：无 baseline，score > 0 即 accept。

设计约束（D7）：门用 EvalResult，不用 LLM 自评
（SkillLens 实测 LLM 自评 46.4% 不可靠）。

【内容摘要】
- __init__(min_delta)                            : 保存最小 score 增量。
- decide(candidate, baseline, eval_result)       : 对外主入口，产 GateDecision。
- _baseline_score(eval_result) -> float          : 从 evidence 的 baseline_pass 比例推断 baseline 分。

【职责边界】
- 只负责：用 EvalResult 做门控决策，产 GateDecision。
- 不负责：评估（eval_bridge）、变异（mutator）、聚焦（focuser）、触发（trigger）、
  持久化（store）、编排（EvolutionManager）。
- 不做 LLM 自评：所有决策基于 EvalResult（D7）。
- 不写 version DAG：create_version 由 EvolutionManager 在 accept 后调。

【INVARIANT】
- hard_failures 非空 → 直接 reject（candidate 改坏了）。
- baseline is None（CAPTURED 新 skill）：
    score > 0 → accept（new_version_id=candidate.skill_id）
    score = 0 → reject
- FIX / DERIVED：candidate.score > baseline_score + min_delta → accept。
- baseline_score 从 evidence 的 baseline_pass 比例推断；无 evidence 返 0.0。
- min_delta 默认 0.0（严格大于即可）。
- accept 时 new_version_id = candidate.skill_id。
- D7：门用 EvalResult 非 LLM 自评（SkillLens 46.4% 不可靠）。
"""
from __future__ import annotations

from poirot.backend.agents.skill.evolution.types import EvalResult, GateDecision
from poirot.backend.agents.skill.types import SkillRecord


class ScoreDeltaGate:
    """candidate score > baseline score 且无 hard_failure → accept。

    构造参数：
    - min_delta : 最小 score 增量（默认 0.0，严格 > 即可）。
    """

    def __init__(self, min_delta: float = 0.0) -> None:
        """保存最小增量阈值。"""
        self._min_delta = min_delta

    def decide(
        self,
        candidate: SkillRecord,
        baseline: SkillRecord,
        eval_result: EvalResult,
    ) -> GateDecision:
        """门控决策（对外主入口）。

        步骤：
            1. hard_failures 非空 → reject（candidate 改坏了）。
            2. baseline is None（CAPTURED）：
                 score > 0 → accept；否则 reject。
            3. FIX / DERIVED：candidate.score > baseline_score + min_delta → accept；
               否则 reject。

        Args:
            candidate:   变异后的候选（is_active=False）。
            baseline:    当前激活版（CAPTURED 时为 None）。
            eval_result: EvalResult（分数 + hard_failures + evidence）。

        Returns:
            GateDecision（accept / reject）。
        """
        # hard_failure → reject（candidate 改坏了）
        if eval_result.hard_failures:
            return GateDecision(
                recommendation="reject",
                reason=f"hard_failures: {eval_result.hard_failures}",
            )
        # CAPTURED 无 baseline（新 skill）→ score>0 即 accept（无 delta 对比）
        if baseline is None:
            if eval_result.score > 0:
                return GateDecision(
                    recommendation="accept",
                    reason=f"CAPTURED score={eval_result.score:.2f}",
                    new_version_id=candidate.skill_id,
                )
            return GateDecision(recommendation="reject", reason="CAPTURED score=0")

        # FIX/DERIVED：candidate score > baseline score + min_delta
        # baseline score 从 evidence 的 baseline_pass 比例推断
        baseline_score = self._baseline_score(eval_result)
        if eval_result.score > baseline_score + self._min_delta:
            return GateDecision(
                recommendation="accept",
                reason=f"candidate={eval_result.score:.2f} > baseline={baseline_score:.2f} + delta={self._min_delta}",
                new_version_id=candidate.skill_id,
            )
        return GateDecision(
            recommendation="reject",
            reason=f"candidate={eval_result.score:.2f} <= baseline={baseline_score:.2f} + delta={self._min_delta}",
        )

    @staticmethod
    def _baseline_score(eval_result: EvalResult) -> float:
        """从 evidence 的 baseline_pass 比例推断 baseline score。

        无 evidence → 返 0.0。

        Args:
            eval_result: EvalResult。

        Returns:
            baseline_score ∈ [0, 1]。
        """
        if not eval_result.evidence:
            return 0.0
        passed = sum(1 for e in eval_result.evidence if e.baseline_pass)
        return passed / len(eval_result.evidence)