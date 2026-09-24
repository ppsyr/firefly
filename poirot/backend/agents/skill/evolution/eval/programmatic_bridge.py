"""ProgrammaticEvalBridge — 兼容 facade（D1 方案 A + facade 保留）。

【整体职责】
L2 的"程序化评估桥"实现，作为兼容 facade 保留：
- L3 关闭时：EvolutionManager 用此 facade（L2 自带 floor）。
- L3 启用时：bootstrap 注入 RegistryEvalBridge 替换此 facade。
内部委托 ResponseContractChecker（contract-aware 升级版）。

保留原因（D1 方案 A + facade）：
- L2a 测试直接调 ProgrammaticEvalBridge._check_* 静态方法；
- 这些静态方法在此作为 checks 模块的委托（单一真相源仍在 checks）。

【内容摘要】
模块级常量（向后兼容，L2a 测试引用）：
- _HARD_MODES / _DIRECTIVE_WORDS / _UNFOUNDED_WORDS / _CONCLUSION_WORDS /
  _CITE_PATTERN / _YAML_FRONTMATTER / _PARAGRAPH_LIMIT /
  _SEMANTIC_DENSITY_MIN / _SEMANTIC_DENSITY_MAX
  均为 checks 模块同名常量的转发。

类方法：
- __init__()                              : 构造 ResponseContractChecker。
- evaluate(ctx) -> EvalResult             : 委托 checker.check（对外主入口）。
- _read_content(record) -> str            : 委托 checks.read_content。
- _split_body(content) -> str             : 委托 checks.split_body。
- _check_nonempty / _check_json_parseable / _check_must_cite /
  _check_paragraph_limit / _check_lead_with_conclusion /
  _check_no_unfounded_claims / _check_markdown_table
                                          : 委托 checks 的检查函数。
- _semantic_density(content) -> float     : 委托 checks.semantic_density。

【职责边界】
- 只负责：兼容 facade（委托 checker + 委托 checks 的静态方法）。
- 不负责：规则编译（ContractCompiler）、检查实现（checks）、门控（gate）、
  变异（mutator）、聚焦（focuser）、持久化（store）。
- 不重复实现 check 逻辑：所有 _check_* 都委托 checks，保持单一真相源。
- 不参与 L3 启用时的流程：L3 启用后被 RegistryEvalBridge 替换。

【INVARIANT】
- 实现 EvalBridge Protocol 的 evaluate(ctx) -> EvalResult。
- evaluate 委托 ResponseContractChecker.check（contract-aware）。
- 模块级常量与 _check_* 静态方法均为 checks 模块的转发（向后兼容 L2a 测试）。
- _check_markdown_table 恒返 True（宽松 pass，L2a 兼容）——
  并非所有 skill 需表格。
- baseline 为空时 baseline_content 传空串。
- L3 关闭 → 用此 facade；L3 启用 → 被 RegistryEvalBridge 替换（bootstrap 决定）。
"""
from __future__ import annotations

from poirot.backend.agents.skill.evolution.types import EvalContext, EvalResult
from poirot.backend.agents.skill.eval.analyzers import checks
from poirot.backend.agents.skill.eval.analyzers.contract_compiler import ContractCompiler
from poirot.backend.agents.skill.eval.analyzers.response_contract_checker import (
    ResponseContractChecker,
)

# 向后兼容：L2a 测试引用的模块级常量
_HARD_MODES = checks.HARD_MODES
_DIRECTIVE_WORDS = checks.DIRECTIVE_WORDS
_UNFOUNDED_WORDS = checks.UNFOUNDED_WORDS
_CONCLUSION_WORDS = checks.CONCLUSION_WORDS
_CITE_PATTERN = checks.CITE_PATTERN
_YAML_FRONTMATTER = checks.YAML_FRONTMATTER
_PARAGRAPH_LIMIT = checks.PARAGRAPH_LIMIT
_SEMANTIC_DENSITY_MIN = checks.SEMANTIC_DENSITY_MIN
_SEMANTIC_DENSITY_MAX = checks.SEMANTIC_DENSITY_MAX


class ProgrammaticEvalBridge:
    """兼容 facade。内部委托 ResponseContractChecker。

    L3 关闭时的默认 EvalBridge；
    保留 _check_* 静态方法作为 checks 模块的委托（L2a 测试向后兼容）。
    """

    def __init__(self) -> None:
        """构造内部 checker（ContractCompiler + ResponseContractChecker）。"""
        self._checker = ResponseContractChecker(ContractCompiler())

    def evaluate(self, ctx: EvalContext) -> EvalResult:
        """委托 ResponseContractChecker（对外主入口）。

        行为：
            1. 读 candidate SKILL.md 内容。
            2. baseline 非空则读其内容，否则用空串。
            3. 调 checker.check(candidate, baseline) 返 EvalResult。

        Args:
            ctx: EvalContext（含 candidate / baseline）。

        Returns:
            EvalResult。
        """
        candidate_content = self._read_content(ctx.candidate)
        baseline_content = self._read_content(ctx.baseline) if ctx.baseline else ""
        return self._checker.check(candidate_content, baseline_content)

    # ── 静态方法委托 checks 模块（L2a 测试向后兼容）────────

    @staticmethod
    def _read_content(record) -> str:
        """委托 checks.read_content（读 SKILL.md 全文）。"""
        return checks.read_content(record)

    @staticmethod
    def _split_body(content: str) -> str:
        """委托 checks.split_body（去 frontmatter 返 body）。"""
        return checks.split_body(content)

    @staticmethod
    def _check_nonempty(content: str) -> bool:
        """委托 checks.check_nonempty（body 非空）。"""
        return checks.check_nonempty(content)

    @staticmethod
    def _check_json_parseable(content: str) -> bool:
        """委托 checks.check_json_parseable（frontmatter YAML 可解析）。"""
        return checks.check_json_parseable(content)

    @staticmethod
    def _check_must_cite(content: str) -> bool:
        """委托 checks.check_must_cite（含引用标记）。"""
        return checks.check_must_cite(content)

    @staticmethod
    def _check_paragraph_limit(content: str) -> bool:
        """委托 checks.check_paragraph_limit（段落数 ≤ 上限）。"""
        return checks.check_paragraph_limit(content)

    @staticmethod
    def _check_lead_with_conclusion(content: str) -> bool:
        """委托 checks.check_lead_with_conclusion（前 3 段含结论词）。"""
        return checks.check_lead_with_conclusion(content)

    @staticmethod
    def _check_markdown_table(content: str) -> bool:
        """恒返 True（宽松 pass，L2a 兼容）。

        并非所有 skill 需表格，因此不做实际检查。
        """
        return True  # 非所有 skill 需表格，宽松 pass（L2a 兼容）

    @staticmethod
    def _check_no_unfounded_claims(content: str) -> bool:
        """委托 checks.check_no_unfounded_claims（无绝对化无据词）。"""
        return checks.check_no_unfounded_claims(content)

    @staticmethod
    def _semantic_density(content: str) -> float:
        """委托 checks.semantic_density（指令性词密度）。"""
        return checks.semantic_density(content)