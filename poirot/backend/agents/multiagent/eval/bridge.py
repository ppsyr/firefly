"""L3 EvalBridge Protocol — L2 调 L3 的唯一入口契约。

【整体职责】
定义 L2 调用 L3 的统一契约（EvalBridge Protocol），并提供一个实现
（OrchestrationBridge）：根据 EvalContext 自动选 adapter、调它、fail-closed 返回
EvalResult。L2 的 PromotionGate 只依赖此契约，不关心 L3 是否启用。

【内容摘要】
- EvalBridge(Protocol, runtime_checkable) : L2 调 L3 的契约（evaluate / list_available_methods / health_check）。
- OrchestrationBridge                     : EvalBridge 的实现（选 adapter + 调用 + fail-closed）。

【职责边界】
- 只负责：定义契约、按 ctx 特征选 adapter、调用 adapter、异常兜底。
- 不负责：具体评估逻辑（adapter 负责）、评估器注册（registry 负责）、
  晋升决策（L2 PromotionGate 负责）。
- 不持有状态：OrchestrationBridge 只持有 registry 引用。

【INVARIANT】
- EvalBridge 是 runtime_checkable Protocol：支持 isinstance 运行时检查。
- 不共享 skill 的 EvalBridge：skill 用 SkillRecord 专属类型，不能跨模块共享。
- evaluate 是同步阻塞调用（与 L1 sync only 一致）。
- fail-closed：adapter 异常 → 返回 EvalResult(success=False, failure_reason=str(exc))，不抛异常。
- adapter 未注册 → 返回 EvalResult(success=False, failure_reason="adapter '<method>' not registered")。
- _select_method 优先级固定：
  1. ctx.eval_method_hint（显式指定）
  2. 全部 task 有 expected_outcome → longitudinal_pairs
  3. ctx.metadata["task_type"] == "open_ended" → llm_judge
  4. 默认 → programmatic
- health_check：registry 至少有 1 个 adapter 注册时为 True。
- 复用 L2 自建 EvalResult（不重复定义）。
"""
from typing import Protocol, runtime_checkable

from poirot.backend.agents.multiagent.eval.registry import SpecialistEvalRegistry
from poirot.backend.agents.multiagent.eval.types import EvalContext
from poirot.backend.agents.multiagent.evolution.promotion_gate import EvalResult


@runtime_checkable
class EvalBridge(Protocol):
    """L2 调 L3 的统一契约。

    实现方：OrchestrationBridge（L3 启用时）+ MultiagentProgrammaticFacade（L3 未启用时）。
    L2 PromotionGate.bridge 参数类型即此 Protocol——L2 不关心具体实现。
    """

    def evaluate(self, ctx: EvalContext) -> EvalResult:
        """同步阻塞评估。

        fail-closed：异常时返回 EvalResult(success=False, failure_reason=...)，不抛异常。

        Args:
            ctx: L2 传的评估上下文（candidate / baseline / task_sample / ...）。

        Returns:
            EvalResult（含 candidate_score / baseline_score / CI / method_used）。
        """
        ...

    def list_available_methods(self) -> tuple[str, ...]:
        """返回已注册的评估方法名 tuple。

        示例：("programmatic", "llm_judge", "longitudinal_pairs")。
        """
        ...

    def health_check(self) -> bool:
        """registry 非空时为 True（至少 1 个 adapter 注册）。"""
        ...


class OrchestrationBridge:
    """L3 评估转接层实现，实现 EvalBridge Protocol。

    L2 调 L3 的唯一入口（注入 PromotionGate.bridge）。
    流程：evaluate(ctx) → _select_method(ctx) 选 adapter → adapter.evaluate(ctx)。
    fail-closed：adapter 异常返回 EvalResult(success=False)，不抛异常。
    """

    def __init__(self, adapter_registry: SpecialistEvalRegistry) -> None:
        """初始化。

        Args:
            adapter_registry: 评估器注册表，提供 get / list_methods。
        """
        self._adapters = adapter_registry

    def evaluate(self, ctx: EvalContext) -> EvalResult:
        """选 adapter → 调用 → fail-closed 兜底。

        流程：
        1. _select_method(ctx) 决定用哪个 method。
        2. registry.get(method)；未注册 → 返回失败（adapter '<method>' not registered）。
        3. adapter.evaluate(ctx)；异常 → 返回失败（failure_reason=str(exc)）。
        """
        method = self._select_method(ctx)
        adapter = self._adapters.get(method)
        if adapter is None:
            return EvalResult(
                candidate_score=0.0, baseline_score=0.0,
                ci_low=0.0, ci_high=0.0, sample_size=0,
                method_used=method, raw_data_ref=None,
                success=False, failure_reason=f"adapter '{method}' not registered",
            )
        try:
            return adapter.evaluate(ctx)
        except Exception as exc:
            return EvalResult(
                candidate_score=0.0, baseline_score=0.0,
                ci_low=0.0, ci_high=0.0, sample_size=0,
                method_used=method, raw_data_ref=None,
                success=False, failure_reason=str(exc),
            )

    def _select_method(self, ctx: EvalContext) -> str:
        """根据 ctx 特征自动选 adapter。

        优先级（从高到低）：
        1. ctx.eval_method_hint（显式指定）。
        2. 全部 task 有 expected_outcome → "longitudinal_pairs"。
        3. ctx.metadata["task_type"] == "open_ended" → "llm_judge"。
        4. 默认 → "programmatic"。
        """
        if ctx.eval_method_hint:
            return ctx.eval_method_hint
        if ctx.task_sample and all(t.expected_outcome for t in ctx.task_sample):
            return "longitudinal_pairs"
        if ctx.metadata.get("task_type") == "open_ended":
            return "llm_judge"
        return "programmatic"

    def list_available_methods(self) -> tuple[str, ...]:
        """返回 registry 里已注册的方法名 tuple。"""
        return self._adapters.list_methods()

    def health_check(self) -> bool:
        """registry 至少有 1 个 adapter 注册时为 True。"""
        return len(self._adapters.list_methods()) > 0