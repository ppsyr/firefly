"""EvalRegistry + RegistryEvalBridge — L2 唯一看见的 L3 实现。

【整体职责】
向 L2（evolution）暴露 L3 评估层的唯一入口。
- EvalRegistry        ：实例级注册表，持有评估组件（当前是 contract checker）。
- RegistryEvalBridge  ：实现 L2 的 EvalBridge Protocol，内部调 ResponseContractChecker。
- build_default_registry：默认装配工厂（bootstrap 用）。

设计约束：
- D-L3-2：EvalBridge Protocol 是 L2 已有约定；RegistryEvalBridge 替换旧的
  ProgrammaticEvalBridge，L2 不感知 L3 内部变化。
- D-L3-8：fail-closed —— registry 为空 / checker 抛异常 → 返回 reject 的
  EvalResult，绝不"裸跑"（D8 安全原则）。
- 注册是实例级（不做 class-level 全局注册），由 bootstrap 装配。

【内容摘要】
- EvalRegistry              : 实例级注册表，持有 ResponseContractChecker。
    - __init__(contract_checker)
    - get_contract_checker() -> ResponseContractChecker
- RegistryEvalBridge        : L2 唯一可见的 L3 实现（EvalBridge Protocol）。
    - __init__(registry)
    - evaluate(ctx) -> EvalResult    : 主入口；异常 fail-closed reject。
- build_default_registry()  : 装配默认 registry（ContractCompiler + Checker）。

【职责边界】
- 只负责：向 L2 暴露评估入口、分发到 contract checker、异常 fail-closed。
- 不负责：具体规则编译（ContractCompiler）、规则检查逻辑（ResponseContractChecker）、
  其他层评估（judgment / task judge / tracker 不由本桥调用）。
- 不做全局状态：registry 为实例级，避免 class-level 全局注册。
- L2 不需要知道 L3 的实现类：只通过 EvalBridge Protocol 调 evaluate。

【INVARIANT】
- EvalBridge Protocol 保持 L2 已有约定（D-L3-2）；RegistryEvalBridge 是其 L3 实现。
- fail-closed：任何异常 → EvalResult(recommendation="reject", hard_failures=("eval_exception",))。
- registry 实例级，bootstrap 装配，无 class-level 全局注册。
- baseline 为空时，baseline_content 传空串（不阻断检查）。
- build_default_registry 是默认装配路径，bootstrap 用它构造 registry。
"""
from __future__ import annotations

from pathlib import Path

from poirot.backend.agents.skill.evolution.types import EvalContext, EvalEvidence, EvalResult
from poirot.backend.agents.skill.eval.analyzers import checks
from poirot.backend.agents.skill.eval.analyzers.contract_compiler import ContractCompiler
from poirot.backend.agents.skill.eval.analyzers.response_contract_checker import (
    ResponseContractChecker,
)


class EvalRegistry:
    """实例级 registry，bootstrap 装配。

    与 class-level 全局注册的区别：每个实例持有自己的组件，
    避免多实例/多测试之间互相污染。当前只注册 contract checker。

    方法：
    - get_contract_checker() -> ResponseContractChecker：取注册的检查器。
    """

    def __init__(self, contract_checker: ResponseContractChecker) -> None:
        """保存 contract checker（唯一注册组件）。

        Args:
            contract_checker: 响应层契约检查器。
        """
        self._contract_checker = contract_checker

    def get_contract_checker(self) -> ResponseContractChecker:
        """返回注册的 ResponseContractChecker。"""
        return self._contract_checker


class RegistryEvalBridge:
    """L2 唯一看见的 L3 实现，实现 EvalBridge Protocol。

    职责：把 L2 的 evaluate(ctx) 调用转成对 ResponseContractChecker 的调用，
    并在任何异常时 fail-closed（返回 reject），保证 L2 不会在评估缺失时"裸跑"。

    方法：
    - evaluate(ctx) -> EvalResult：主入口。
    """

    def __init__(self, registry: EvalRegistry) -> None:
        """保存 registry（从中取 checker）。

        Args:
            registry: 实例级注册表。
        """
        self._registry = registry

    def evaluate(self, ctx: EvalContext) -> EvalResult:
        """调 ResponseContractChecker 产 EvalResult；异常 fail-closed reject。

        步骤：
            1. 从 registry 取 checker。
            2. 读 candidate 内容（checks.read_content）。
            3. baseline 存在则读其内容，否则用空串。
            4. 调 checker.check(candidate, baseline) 返回结果。
            5. 任何异常 → 返回 reject 的 EvalResult（fail-closed）。

        Args:
            ctx: EvalContext，含 candidate / baseline 路径。

        Returns:
            EvalResult；异常时为 score=0.0 / recommendation="reject"。
        """
        try:
            checker = self._registry.get_contract_checker()
            candidate_content = checks.read_content(ctx.candidate)
            baseline_content = (
                checks.read_content(ctx.baseline)
                if ctx.baseline
                else ""
            )
            return checker.check(candidate_content, baseline_content)
        except Exception as exc:
            return EvalResult(
                score=0.0,
                metric="hard",
                hard_failures=("eval_exception",),
                evidence=(
                    EvalEvidence(
                        kind="programmatic_rule",
                        rule_name="eval_bridge",
                        baseline_pass=False,
                        candidate_pass=False,
                        detail=str(exc),
                    ),
                ),
                confidence=0.0,
                recommendation="reject",
            )


def build_default_registry() -> EvalRegistry:
    """构建默认 registry（含 ContractCompiler + ResponseContractChecker）。

    供 bootstrap 装配使用：把默认的 compiler 与 checker 组装成 registry。

    Returns:
        含默认 checker 的 EvalRegistry。
    """
    checker = ResponseContractChecker(ContractCompiler())
    return EvalRegistry(checker)