"""ResponseContractChecker — 响应层 eval（pre-promotion，检查 candidate SKILL.md）。

【整体职责】
响应层评估器：在 candidate 晋升前，用 ContractCompiler 编译出适用规则，
跑在 candidate 上，汇总为 EvalResult（score + hard_failures + recommendation）。
- 规则来源：ContractCompiler.compile(candidate_content)（按 skill 文本动态编译）。
- 检查执行：_run_rule 分发到 checks 模块的确定性检查函数。
- 结论：有 hard 规则失败 → reject；否则 accept。

【内容摘要】
类方法：
- __init__(compiler)                          : 保存 compiler（缺省新建）。
- check(candidate_content, baseline_content)  : 对外主入口，产 EvalResult。
- _run_rule(rule_id, content, params)         : 按 rule_id 分发到 checks 检查函数。

【职责边界】
- 只负责：编译规则 → 逐条执行 → 汇总打分 → 产 EvalResult。
- 不负责：规则编译逻辑（ContractCompiler）、具体检查实现（checks.py）、
  LLM 评估（analyzer / judge）、持久化（store）。
- 不做 hard 判定以外的决策：是否晋升由 L2 消费 EvalResult 决定。
- 不 short-circuit：逐条跑完所有规则再汇总（hard_failures 只记录，不提前返回）。

【INVARIANT】
- D-L3-4：contract-aware 替代 L2a ProgrammaticEvalBridge 固定 7 mode。
- 复用 L2a ProgrammaticEvalBridge 的 _check_* 静态方法（单一真相源，不重复实现）。
- baseline 为空时 base_pass 视为 True（不阻断，仅作对照记录）。
- score = passed / total；total=0 时 score=0.0。
- recommendation = "reject" if hard_failures else "accept"。
- confidence 固定 0.7（programmatic 检查的置信度）。
- 未知 rule_id 默认 pass（True），保证向前兼容。
- semantic_density 是唯一"区间判定"：在 _run_rule 内二次判断（checks 只返 float）。
"""
from __future__ import annotations

from poirot.backend.agents.skill.evolution.types import (
    EvalEvidence,
    EvalResult,
)
from poirot.backend.agents.skill.eval.analyzers import checks
from poirot.backend.agents.skill.eval.analyzers.contract_compiler import ContractCompiler


class ResponseContractChecker:
    """响应层 eval：编译规则 + 跑 candidate + 产 EvalResult。

    依赖：
    - compiler: ContractCompiler；缺省时自动新建。
    """

    def __init__(self, compiler: ContractCompiler | None = None) -> None:
        """保存 compiler。

        Args:
            compiler: 规则编译器；None 时新建默认 ContractCompiler。
        """
        self._compiler = compiler or ContractCompiler()

    def check(
        self,
        candidate_content: str,
        baseline_content: str,
    ) -> EvalResult:
        """跑编译规则，返 EvalResult（对外主入口）。

        步骤：
            1. rules = compiler.compile(candidate_content)。
            2. 逐条执行：
                 cand_pass = _run_rule(rule_id, candidate_content, params)
                 base_pass = _run_rule(rule_id, baseline_content, params)（无 baseline → True）
                 cand_pass → passed += 1
                 rule.hard 且 not cand_pass → hard_failures.append(rule_id)
                 evidence.append(EvalEvidence(...))
            3. score = passed / total（total=0 → 0.0）。
            4. recommendation = "reject" if hard_failures else "accept"。
            5. 返回 EvalResult(score, metric="hard", hard_failures, evidence,
                              confidence=0.7, recommendation)。

        Args:
            candidate_content: 候选 SKILL.md 全文。
            baseline_content:  基线 SKILL.md 全文（可为空串）。

        Returns:
            EvalResult。
        """
        rules = self._compiler.compile(candidate_content)

        evidence: list[EvalEvidence] = []
        hard_failures: list[str] = []
        passed = 0
        total = 0

        for rule in rules:
            total += 1
            cand_pass = self._run_rule(rule.rule_id, candidate_content, rule.params)
            base_pass = self._run_rule(rule.rule_id, baseline_content, rule.params) if baseline_content else True

            if cand_pass:
                passed += 1
            if rule.hard and not cand_pass:
                hard_failures.append(rule.rule_id)

            evidence.append(EvalEvidence(
                kind="programmatic_rule",
                rule_name=rule.rule_id,
                baseline_pass=base_pass,
                candidate_pass=cand_pass,
            ))

        score = passed / total if total else 0.0
        recommendation = "reject" if hard_failures else "accept"

        return EvalResult(
            score=score,
            metric="hard",
            hard_failures=tuple(hard_failures),
            evidence=tuple(evidence),
            confidence=0.7,
            recommendation=recommendation,  # type: ignore[arg-type]
        )

    @staticmethod
    def _run_rule(rule_id: str, content: str, params: dict) -> bool:
        """按 rule_id 分发到 checks 模块的检查函数。

        分发规则：
        - "nonempty"             → checks.check_nonempty
        - "json_parseable"       → checks.check_json_parseable
        - "must_cite"            → checks.check_must_cite
        - "lead_with_conclusion" → checks.check_lead_with_conclusion
        - "paragraph_limit"      → checks.check_paragraph_limit
        - "no_unfounded_claims"  → checks.check_no_unfounded_claims
        - "semantic_density"     → checks.semantic_density + 区间判定
                                   （content 为空 → False）
        - 未知 rule_id           → True（默认 pass，向前兼容）

        Args:
            rule_id: 规则标识。
            content: 被检查的文本。
            params:  规则参数（当前分发逻辑未消费，保留给未来规则）。

        Returns:
            该规则是否通过（bool）。
        """
        if rule_id == "nonempty":
            return checks.check_nonempty(content)
        if rule_id == "json_parseable":
            return checks.check_json_parseable(content)
        if rule_id == "must_cite":
            return checks.check_must_cite(content)
        if rule_id == "lead_with_conclusion":
            return checks.check_lead_with_conclusion(content)
        if rule_id == "paragraph_limit":
            return checks.check_paragraph_limit(content)
        if rule_id == "no_unfounded_claims":
            return checks.check_no_unfounded_claims(content)
        if rule_id == "semantic_density":
            if not content:
                return False
            density = checks.semantic_density(content)
            return checks.SEMANTIC_DENSITY_MIN <= density <= checks.SEMANTIC_DENSITY_MAX
        return True  # 未知规则默认 pass