"""L3 SpecialistEvalRegistry + EvalAdapter Protocol。

【整体职责】
定义 L3 评估器的契约与注册表：EvalAdapter Protocol 规定评估器必须实现的方法；
SpecialistEvalRegistry 以实例级 dict 管理多个评估器，供 Bridge 按 method 名取用。

【内容摘要】
- EvalAdapter(Protocol, runtime_checkable) : 评估器契约（evaluate + health_check）。
- SpecialistEvalRegistry                  : 实例级注册表（register / get / list_methods）。

【职责边界】
- 只负责：定义评估器契约、注册与查询评估器。
- 不负责：评估逻辑实现（3 个 adapter 负责）、评估触发（Bridge 负责）、
  装配（bootstrap 负责）。
- 不持有状态：registry 持有 dict，但本身不做全局注册。

【INVARIANT】
- 不做 class-level 全局注册：registry 为实例级，遵循 Poirot 禁全局单例原则。
- EvalAdapter 是 runtime_checkable Protocol：支持 isinstance 运行时检查。
- registry.register 重复注册覆盖（后注册生效）。
- registry.get 未注册返回 None（不抛异常）。
- list_methods 按注册顺序返回 tuple。
- 复用 L2 自建 EvalResult（不重复定义）。
- 3 个 adapter 各自独立实现：programmatic / llm_judge / longitudinal_pairs。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from poirot.backend.agents.multiagent.eval.types import EvalContext
from poirot.backend.agents.multiagent.evolution.promotion_gate import EvalResult


@runtime_checkable
class EvalAdapter(Protocol):
    """L3 评估器契约。

    实现方：ProgrammaticAdapter / LLMJudgeAdapter / LongitudinalPairsAdapter。
    由 SpecialistEvalRegistry 统一管理（register / get / list_methods）。
    """

    def evaluate(self, ctx: EvalContext) -> EvalResult:
        """评估 candidate vs baseline，返回 EvalResult（含 CI + success + method_used）。"""
        ...

    def health_check(self) -> bool:
        """评估器健康检查。

        示例：LLM-judge 检查 model 是否可用；programmatic 检查 ResultSummarizer 是否可用。
        """
        ...


class SpecialistEvalRegistry:
    """实例级注册表，管理多个 EvalAdapter，由 bootstrap 装配。

    不做 class-level 全局注册（遵循 Poirot 禁全局单例原则）。
    由 OrchestrationBridge 接收，evaluate 时按 method 名取 adapter。
    """

    def __init__(self) -> None:
        self._adapters: dict[str, EvalAdapter] = {}

    def register(self, method: str, adapter: EvalAdapter) -> None:
        """注册 adapter。

        method 名如 "programmatic" / "llm_judge" / "longitudinal_pairs"。
        重复注册覆盖（后注册的生效，按 bootstrap 装配顺序）。
        """
        self._adapters[method] = adapter

    def get(self, method: str) -> EvalAdapter | None:
        """按 method 名取 adapter；未注册返回 None。"""
        return self._adapters.get(method)

    def list_methods(self) -> tuple[str, ...]:
        """已注册 method 名 tuple（按注册顺序）。"""
        return tuple(self._adapters.keys())