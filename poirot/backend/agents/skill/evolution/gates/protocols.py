"""门控 Protocol（2b/L3 预留）。

【整体职责】
定义 evolution 闭环"门控"环节的 Protocol（接口契约），为 2b / L3 预留。
2a 已实现 ScoreDeltaGate + GitRatchet（gates/score_delta_gate.py + gates/git_ratchet.py）；
本模块的 Protocol 用于未来替换 / 扩展，实现时零侵入。

5 个 Protocol：
- ChampionGateProtocol  （2b）：score delta + hard_failures 双门（区分关键 vs 非关键）。
- HITLGateProtocol      （2b）：人审装饰器，accept 转 pending_human staging。
- CompositeGateProtocol （2b）：cascading 链式，早期 reject short-circuit。
- ValidationGateProtocol（L3）：held-out 重放 + longitudinal pairs，
                                需 L3 ValidationGateEvalAdapter。
- MultiJudgeGateProtocol（L3）：多 LLM judge + majority vote，
                                需 L3 LLMJudgeEvalAdapter。

所有门实现 PromotionGate Protocol（evolution/protocols.py）。

【内容摘要】
- ChampionGateProtocol  : decide(candidate, baseline, eval_result) -> GateDecision。
- HITLGateProtocol      : 同上（人审装饰）。
- CompositeGateProtocol : 同上（链式组合）。
- ValidationGateProtocol: 同上（held-out 重放）。
- MultiJudgeGateProtocol: 同上（多 judge 投票）。

【职责边界】
- 只定义接口（Protocol），不含实现。
- 不负责：门控决策逻辑（在实现类里）、评估（eval_bridge）、变异（mutator）、
  持久化（store）。
- 2a 已实现的门（ScoreDeltaGate / GitRatchet）不在本模块；
  本模块仅列 2b / L3 预留的 Protocol。
- 不 runtime import 实现类：实现由 bootstrap 注入。

【INVARIANT】
- 5 个 Protocol 全部 @runtime_checkable。
- 全部实现 PromotionGate Protocol（evolution/protocols.py）的 decide 签名。
- decide 统一签名：(candidate, baseline, eval_result) -> GateDecision。
- 2b 预留：ChampionGate / HITLGate / CompositeGate。
- L3 预留：ValidationGate（需 ValidationGateEvalAdapter）/
  MultiJudgeGate（需 LLMJudgeEvalAdapter）。
- CompositeGate 语义：cascading 链式，早期 reject short-circuit。
- HITLGate 语义：accept 转 pending_human，staging 待 /skill approve|reject。
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from poirot.backend.agents.skill.evolution.types import EvalResult, GateDecision
from poirot.backend.agents.skill.types import SkillRecord


@runtime_checkable
class ChampionGateProtocol(Protocol):
    """2b：score delta + hard_failures 双门（区分关键 vs 非关键 hard_failure）。

    方法：
    - decide(candidate, baseline, eval_result) -> GateDecision
    """

    def decide(
        self, candidate: SkillRecord, baseline: SkillRecord, eval_result: EvalResult,
    ) -> GateDecision: ...


@runtime_checkable
class HITLGateProtocol(Protocol):
    """2b：人审装饰器。accept 转 pending_human，staging 待 /skill approve|reject。

    方法：
    - decide(candidate, baseline, eval_result) -> GateDecision
    """

    def decide(
        self, candidate: SkillRecord, baseline: SkillRecord, eval_result: EvalResult,
    ) -> GateDecision: ...


@runtime_checkable
class CompositeGateProtocol(Protocol):
    """2b：cascading 链式组合，早期 reject short-circuit。

    方法：
    - decide(candidate, baseline, eval_result) -> GateDecision
    """

    def decide(
        self, candidate: SkillRecord, baseline: SkillRecord, eval_result: EvalResult,
    ) -> GateDecision: ...


@runtime_checkable
class ValidationGateProtocol(Protocol):
    """L3：held-out 重放 + longitudinal pairs。需 L3 ValidationGateEvalAdapter。

    方法：
    - decide(candidate, baseline, eval_result) -> GateDecision
    """

    def decide(
        self, candidate: SkillRecord, baseline: SkillRecord, eval_result: EvalResult,
    ) -> GateDecision: ...


@runtime_checkable
class MultiJudgeGateProtocol(Protocol):
    """L3：多 LLM judge + majority vote。需 L3 LLMJudgeEvalAdapter。

    方法：
    - decide(candidate, baseline, eval_result) -> GateDecision
    """

    def decide(
        self, candidate: SkillRecord, baseline: SkillRecord, eval_result: EvalResult,
    ) -> GateDecision: ...