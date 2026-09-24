"""EvolutionManager — 自进化闭环编排。

【整体职责】
evolution（L2 进化层）的编排门面。
把 trigger → focus → mutate → eval → gate → create_version/record 串成闭环，
并对外暴露三个入口：
- run_cycle   : 扫所有 trigger，跑一轮自进化（自动）。
- evolve_skill: 手动 FIX（/skill evolve）。
- capture_skill: 手动 CAPTURED（/skill capture）。

2a 为同步触发；asyncio 后台留到 2b（todo_docs/03 §3.8）。

【内容摘要】
- __init__(store, triggers, focuser, mutator, eval_bridge, gate, llm, journal)
      注入闭环各环节组件。
- run_cycle() -> list[EvolutionRecord]
      遍历 triggers，对每个 EvolutionContext 跑 _run_evolution；
      anti-loop：若有 mark_evolved 则调用。
- evolve_skill(skill_name) -> EvolutionRecord
      手动 FIX：取 active skill → 构造 EvolutionContext(METRIC/FIX) → 跑闭环。
- capture_skill(pattern, suggested_name) -> EvolutionRecord
      手动 CAPTURED：优先用 CaptureTrigger.manual_capture，否则自造上下文。
- _run_evolution(ctx) -> EvolutionRecord | None
      单次进化闭环：focus → mutate → eval → gate → create_version → record → journal。
- _emit_journal(ctx, decision, rec)
      发 journal 事件：skill.evolve / skill.captured / skill.evolve_rejected。

【职责边界】
- 只负责：编排闭环、调各 Protocol 实现、落库记录、发 journal。
- 不负责：触发逻辑（trigger）、聚焦逻辑（focuser）、变异逻辑（mutator）、
  评估逻辑（eval_bridge）、门控逻辑（gate）——它只调它们。
- 不做决策：accept / reject 由 gate 决定；本类只根据 decision 执行后续动作。
- 2a 只支持 FIX + CAPTURED（DERIVED 留 2b）。

【INVARIANT】
- 2a 同步触发；asyncio 后台留 2b（todo_docs/03 §3.8）。
- run_cycle 遍历所有 triggers；每个 trigger 可返回多个 EvolutionContext。
- anti-loop：若 trigger 有 mark_evolved，则对该 skill 标记已进化
  （传 name + total_selections）。
- evolve_skill：skill 不存在 → 抛 ValueError；闭环无记录 → 抛 RuntimeError。
- capture_skill：优先复用已注册的 CaptureTrigger.manual_capture；
  否则自造 EvolutionContext。
- create_version 仅在 decision.recommendation ∈ {accept, accept_new_best} 时执行；
  parent_id 为 baseline.skill_id（无 baseline 时 ""）。
- EvolutionRecord 无论 accept / reject 都记录（可审计）。
- 落库与 journal 异常均 silent（pass），不阻断闭环。
- EvalContext.baseline 在无 target 时用 candidate 占位（CAPTURED 场景）。
"""
from __future__ import annotations

import uuid
from typing import Any

from poirot.backend.agents.journal.events import utc_now_iso
from poirot.backend.agents.skill.evolution.types import (
    EvalContext,
    EvolutionContext,
    EvolutionRecord,
)
from poirot.backend.agents.skill.types import SkillRecord


class EvolutionManager:
    """编排：trigger → focus → mutate → eval → gate → create_version/record + journal。

    2a 同步。支持 FIX + CAPTURED（DERIVED 留 2b）。

    依赖（全部由 bootstrap 注入）：
    - store       : 技能存储（提供 get_active / get_metrics /
                    create_version / record_evolution）。
    - triggers    : Trigger 列表。
    - focuser     : FailureFocuser。
    - mutator     : Mutator。
    - eval_bridge : EvalBridge（L2 ProgrammaticEvalBridge / L3 RegistryEvalBridge）。
    - gate        : PromotionGate。
    - llm         : 语言模型（可 None，透传给 mutator）。
    - journal     : 运行日志（可 None，发进化事件）。
    """

    def __init__(
        self,
        store: Any,
        triggers: list[Any],
        focuser: Any,
        mutator: Any,
        eval_bridge: Any,
        gate: Any,
        llm: Any | None = None,
        journal: Any | None = None,
    ) -> None:
        """保存闭环各环节组件（全部由 bootstrap 装配）。"""
        self._store = store
        self._triggers = triggers
        self._focuser = focuser
        self._mutator = mutator
        self._eval_bridge = eval_bridge
        self._gate = gate
        self._llm = llm
        self._journal = journal

    def run_cycle(self) -> list[EvolutionRecord]:
        """扫所有 trigger，跑一轮自进化。返本轮所有 EvolutionRecord。

        步骤：
            1. 遍历 triggers，逐个调 should_trigger(store) 取 contexts。
            2. 对每个 ctx 调 _run_evolution；有记录则收集。
            3. anti-loop：若 trigger 有 mark_evolved，则标记该 skill 已进化
               （传 name + total_selections）。

        Returns:
            list[EvolutionRecord]（本轮所有成功产出的记录）。
        """
        records: list[EvolutionRecord] = []
        for trigger in self._triggers:
            contexts = trigger.should_trigger(self._store)
            for ctx in contexts:
                rec = self._run_evolution(ctx)
                if rec is not None:
                    records.append(rec)
                # anti-loop：MetricMonitorTrigger 标记已进化
                if ctx.target_skill is not None and hasattr(trigger, "mark_evolved"):
                    trigger.mark_evolved(ctx.target_skill.name, ctx.target_skill.total_selections)
        return records

    def evolve_skill(self, skill_name: str) -> EvolutionRecord:
        """手动触发单 skill FIX 进化（/skill evolve）。

        Args:
            skill_name: 目标 skill 名。

        Returns:
            EvolutionRecord。

        Raises:
            ValueError:   skill 不存在。
            RuntimeError: 闭环未产出记录。
        """
        rec = self._store.get_active(skill_name)
        if rec is None:
            raise ValueError(f"skill not found: {skill_name}")
        ctx = EvolutionContext(
            trigger="METRIC",
            evolution_type="FIX",
            target_skill=rec,
            fix_direction="手动触发进化",
        )
        result = self._run_evolution(ctx)
        if result is None:
            raise RuntimeError("evolution produced no record")
        return result

    def capture_skill(self, pattern: str, suggested_name: str) -> EvolutionRecord:
        """手动 CAPTURED 沉淀新 skill（/skill capture）。

        优先用已注册的 CaptureTrigger.manual_capture；
        未注册时自造 EvolutionContext（CAPTURED / 无 target_skill）。

        Args:
            pattern:        可复用模式描述。
            suggested_name: 建议 skill name。

        Returns:
            EvolutionRecord。

        Raises:
            RuntimeError: 闭环未产出记录。
        """
        # 优先用 CaptureTrigger.manual_capture（若注册）
        from poirot.backend.agents.skill.evolution.triggers.capture_trigger import CaptureTrigger
        ctx: EvolutionContext | None = None
        for t in self._triggers:
            if isinstance(t, CaptureTrigger):
                ctx = t.manual_capture(pattern, suggested_name)
                break
        if ctx is None:
            ctx = EvolutionContext(
                trigger="CAPTURE",
                evolution_type="CAPTURED",
                target_skill=None,
                capture_pattern=pattern,
                suggested_name=suggested_name,
            )
        result = self._run_evolution(ctx)
        if result is None:
            raise RuntimeError("capture produced no record")
        return result

    def _run_evolution(self, ctx: EvolutionContext) -> EvolutionRecord | None:
        """跑单次进化闭环。返 EvolutionRecord（accept / reject 均记）。

        步骤：
            1. focus  ：ctx = focuser.focus(ctx, store)。
            2. mutate ：candidate, diff = mutator.mutate(ctx, llm)。
            3. eval   ：构造 EvalContext（baseline / candidate / metrics_baseline）
                        → eval_bridge.evaluate(...)。
            4. gate   ：gate.decide(candidate, baseline, eval_result) → decision。
            5. promote：若 accept / accept_new_best → store.create_version。
            6. record ：构造 EvolutionRecord → store.record_evolution。
            7. journal：若 journal 非 None → _emit_journal。

        Args:
            ctx: EvolutionContext（trigger / focuser 产出）。

        Returns:
            EvolutionRecord；本方法不返回 None（保留签名以兼容未来分支）。
        """
        # 1. focus
        ctx = self._focuser.focus(ctx, self._store)
        # 2. mutate
        candidate, diff = self._mutator.mutate(ctx, self._llm)
        # 3. eval
        baseline = ctx.target_skill
        metrics_baseline = None
        if baseline is not None:
            try:
                metrics_baseline = self._store.get_metrics(baseline.skill_id)
            except Exception:
                metrics_baseline = None
        eval_ctx = EvalContext(
            baseline=baseline if baseline is not None else candidate,
            candidate=candidate,
            metrics_baseline=metrics_baseline,
        )
        eval_result = self._eval_bridge.evaluate(eval_ctx)
        # 4. gate
        decision = self._gate.decide(candidate, baseline, eval_result)  # type: ignore[arg-type]
        # 5. create_version if accept
        created_id: str | None = None
        if decision.recommendation in ("accept", "accept_new_best"):
            parent_id = baseline.skill_id if baseline is not None else ""
            try:
                created_id = self._store.create_version(parent_id, candidate, candidate.lineage.origin)
            except Exception:
                created_id = None
        # 6. record
        rec = EvolutionRecord(
            evolution_id=f"evo_{uuid.uuid4().hex[:12]}",
            skill_name=candidate.name,
            evolution_type=ctx.evolution_type,
            trigger=ctx.trigger,
            baseline_id=baseline.skill_id if baseline is not None else None,
            candidate_id=candidate.skill_id,
            failure_focus=ctx.fix_direction or ctx.capture_pattern,
            mutation_diff=diff,
            eval_score=eval_result.score,
            gate_decision=decision.recommendation,
            created_version_id=created_id,
            timestamp=utc_now_iso(),
        )
        try:
            self._store.record_evolution(rec)
        except Exception:
            pass
        # 7. journal
        if self._journal is not None:
            self._emit_journal(ctx, decision, rec)
        return rec

    def _emit_journal(
        self, ctx: EvolutionContext, decision: Any, rec: EvolutionRecord,
    ) -> None:
        """发 journal 事件。

        事件类型：
        - CAPTURED                     → "skill.captured"
        - accept / accept_new_best     → "skill.evolve"
        - 其他（reject / pending_human）→ "skill.evolve_rejected"

        payload：evolution_id / skill_name / evolution_type /
                 eval_score / gate_decision / created_version_id。
        异常 silent（不阻断闭环）。
        """
        if ctx.evolution_type == "CAPTURED":
            event_type = "skill.captured"
        elif decision.recommendation in ("accept", "accept_new_best"):
            event_type = "skill.evolve"
        else:
            event_type = "skill.evolve_rejected"
        try:
            self._journal.append(event_type, {
                "evolution_id": rec.evolution_id,
                "skill_name": rec.skill_name,
                "evolution_type": rec.evolution_type,
                "eval_score": rec.eval_score,
                "gate_decision": rec.gate_decision,
                "created_version_id": rec.created_version_id,
            })
        except Exception:
            pass