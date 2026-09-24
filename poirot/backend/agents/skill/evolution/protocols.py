"""Skill 自进化层 Protocol 抽象 — 2b/L3 零侵入预留。

【整体职责】
定义 evolution（L2 进化层）的 5 个 Protocol（接口契约），规定进化闭环各环节的职责：
- Trigger         : 何时触发进化（产 EvolutionContext 列表）。
- FailureFocuser  : 失败聚焦（增强 EvolutionContext）。
- Mutator         : 变异（产 candidate + diff）。
- EvalBridge      : eval 转接（产 EvalResult；L2 自带 ProgrammaticEvalBridge，L3 替换）。
- PromotionGate   : 门控（产 GateDecision）。

L2/L3 实现 = 实现这些 Protocol + 注入 EvolutionManager，不改动 2a 核心（零侵入）。

【内容摘要】
- Trigger(Protocol)        : should_trigger(store) -> list[EvolutionContext]。
- FailureFocuser(Protocol) : focus(ctx, store) -> EvolutionContext。
- Mutator(Protocol)        : mutate(ctx, llm) -> (SkillRecord, str)。
- EvalBridge(Protocol)     : evaluate(ctx) -> EvalResult。
- PromotionGate(Protocol)  : decide(candidate, baseline, eval_result) -> GateDecision。

【职责边界】
- 只定义接口（Protocol），不含实现。
- 不负责：触发 / 聚焦 / 变异 / 评估 / 门控的具体逻辑（在 triggers / focus /
  mutators / registry / gates 的实现里）。
- 不 runtime import 实现类：实现由 bootstrap 注入。
- 全部 runtime_checkable：支持 isinstance 检查（便于 bootstrap 装配校验）。

【INVARIANT】
- 5 个 Protocol 全部 @runtime_checkable。
- Trigger.should_trigger 返回列表：一次可产多个进化上下文（多个 skill 同时满足条件）。
- FailureFocuser.focus 返回增强后的 EvolutionContext（输入 ctx 是 raw context）。
- Mutator.mutate 产出的 candidate 是 is_active=False 的 SkillRecord；
  第二返回值为 mutation diff（文本）。
- Mutator 受 budget / 粒度 / 维度约束（由实现类保证）。
- EvalBridge 是"第二层 ↔ 第三层"的转接点：
    L2 自带 ProgrammaticEvalBridge 作为 floor；
    L3 替换为 RegistryEvalBridge（D-L3-2）。
- PromotionGate.decide 用 EvalResult 决策 accept / reject / pending_human。
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from poirot.backend.agents.skill.evolution.types import (
    EvalContext,
    EvalResult,
    EvolutionContext,
    GateDecision,
)
from poirot.backend.agents.skill.types import SkillRecord


@runtime_checkable
class Trigger(Protocol):
    """触发器：读 store 的 metrics / 状态，产出待进化的上下文列表。

    方法：
    - should_trigger(store) -> list[EvolutionContext]
        一次可返回多个上下文（多个 skill 同时满足触发条件）。
    """

    def should_trigger(self, store: Any) -> list[EvolutionContext]: ...


@runtime_checkable
class FailureFocuser(Protocol):
    """失败聚焦：输入 raw context，输出聚焦后 context + 修复方向。

    方法：
    - focus(ctx, store) -> EvolutionContext
        增强 ctx：补 failure_evidence / fix_direction 等。
    """

    def focus(self, ctx: EvolutionContext, store: Any) -> EvolutionContext: ...


@runtime_checkable
class Mutator(Protocol):
    """变异器：受 budget / 粒度 / 维度约束，产出 candidate + diff。

    方法：
    - mutate(ctx, llm) -> tuple[SkillRecord, str]
        返回 (candidate, mutation_diff)；
        candidate 为 is_active=False 的 SkillRecord。
    """

    def mutate(self, ctx: EvolutionContext, llm: Any | None) -> tuple[SkillRecord, str]: ...


@runtime_checkable
class EvalBridge(Protocol):
    """第二层 ↔ 第三层 eval 转接。

    L2 自带 ProgrammaticEvalBridge 作为 floor；L3 替换为 RegistryEvalBridge。

    方法：
    - evaluate(ctx) -> EvalResult
    """

    def evaluate(self, ctx: EvalContext) -> EvalResult: ...


@runtime_checkable
class PromotionGate(Protocol):
    """提升门：用 EvalResult 决策 accept / reject / pending_human。

    方法：
    - decide(candidate, baseline, eval_result) -> GateDecision
    """

    def decide(
        self,
        candidate: SkillRecord,
        baseline: SkillRecord,
        eval_result: EvalResult,
    ) -> GateDecision: ...