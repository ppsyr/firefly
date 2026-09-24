"""MetricMonitorTrigger — 周期 metric 扫描触发（借鉴 OpenSpace Trigger 3）。

【整体职责】
METRIC 类进化的触发器：周期扫描 active skills 的 metrics，
诊断出"需要 FIX 的 skill"，产出 EvolutionContext 列表。

两阶段筛选：
- Phase 1 规则（_diagnose_skill_health，阈值宽松）：
    fallback_rate > 0.4                          → FIX（常选不用，指令不清）
    applied_rate > 0.4 且 completion_rate < 0.35 → FIX（用了没完成，指令错）
    effective_rate < 0.55 且 applied_rate > 0.25 → DERIVED（中等效果，2b）
- Phase 2 LLM 确认（llm 非 None 时调，过滤误报；None 跳过）

anti-loop：
- min_selections：total_selections < min 不触发（新进化 skill selections=0）。
- cooldown：自上次进化后需 cooldown_turns 次新 selections 才重评
  （数据驱动，无时间计数）。

【内容摘要】
模块常量（阈值，借鉴 OpenSpace，宽松——LLM 确认过滤误报）：
- _FALLBACK_THRESHOLD          = 0.4
- _LOW_COMPLETION_THRESHOLD    = 0.35
- _HIGH_APPLIED_FOR_FIX        = 0.4
- _MODERATE_EFFECTIVE_THRESHOLD = 0.55
- _MIN_APPLIED_FOR_DERIVED     = 0.25

类方法：
- __init__(threshold, min_selections, cooldown_turns, llm)
- should_trigger(store) -> list[EvolutionContext]         : 扫 active skills，产 FIX 上下文。
- mark_evolved(skill_name, total_selections)              : 记 anti-loop 锚点。
- _diagnose_skill_health(record) -> (type | None, direction) : 规则诊断（Phase 1）。
- _llm_confirm_evolution(record, direction) -> bool       : LLM 确认（Phase 2）。

【职责边界】
- 只负责：扫 metrics + 规则诊断 + LLM 确认 + 产 EvolutionContext。
- 不负责：聚焦（focuser）、变异（mutator）、评估（eval_bridge）、门控（gate）、
  持久化（store）、编排（EvolutionManager）。
- 2a 只产 FIX；DERIVED 留 2b。

【INVARIANT】
- 两阶段：Phase 1 规则（宽松阈值）+ Phase 2 LLM 确认（llm None 时跳过）。
- anti-loop #1：total_selections < min_selections 不触发。
- anti-loop #2：total_selections - last_evolve_selections < cooldown_turns 不触发
  （数据驱动，非时间计数）。
- 只扫 enabled 的 active skill。
- 2a 只产 FIX 类型；DERIVED 被显式过滤。
- LLM 确认失败 → 保守不触发（返 False）。
- mark_evolved 由 EvolutionManager 在进化完成后调用，记录锚点。
"""
from __future__ import annotations

from typing import Any

from poirot.backend.agents.skill.evolution.types import EvolutionContext
from poirot.backend.agents.skill.types import SkillRecord

# 阈值（借鉴 OpenSpace，宽松——LLM 确认过滤误报）
_FALLBACK_THRESHOLD = 0.4
_LOW_COMPLETION_THRESHOLD = 0.35
_HIGH_APPLIED_FOR_FIX = 0.4
_MODERATE_EFFECTIVE_THRESHOLD = 0.55
_MIN_APPLIED_FOR_DERIVED = 0.25


class MetricMonitorTrigger:
    """周期扫 metrics：effective_rate < threshold AND selections >= min AND cooldown 过。

    产出 FIX 类型 EvolutionContext（2a；DERIVED 留 2b）。

    构造参数：
    - threshold      : effective_rate 触发阈值（默认 0.3）。
    - min_selections : 最少 selections（anti-loop，默认 5）。
    - cooldown_turns : 冷却所需的新 selections 数（默认 10）。
    - llm            : 用于 Phase 2 确认（可 None，跳过确认）。
    """

    def __init__(
        self,
        threshold: float = 0.3,
        min_selections: int = 5,
        cooldown_turns: int = 10,
        llm: Any | None = None,
    ) -> None:
        """保存阈值与 LLM；初始化 anti-loop 锚点表。"""
        self._threshold = threshold
        self._min_selections = min_selections
        self._cooldown_turns = cooldown_turns
        self._llm = llm
        # anti-loop：skill_name → 上次进化时的 total_selections
        self._last_evolve_selections: dict[str, int] = {}

    def should_trigger(self, store: Any) -> list[EvolutionContext]:
        """扫所有 active skill，产出需进化的 FIX context 列表（对外主入口）。

        步骤（对每个 active skill）：
            1. 跳过未 enabled 的。
            2. anti-loop：total_selections < min_selections → 跳过。
            3. anti-loop：total_selections - last < cooldown_turns → 跳过。
            4. Phase 1 规则诊断：_diagnose_skill_health → (evo_type, direction)；
               健康 / DERIVED（2a 不支持）→ 跳过。
            5. Phase 2 LLM 确认（llm 非 None 时）：
               _llm_confirm_evolution 为 False → 跳过。
            6. 收集 EvolutionContext(METRIC, FIX, target_skill, fix_direction)。

        Args:
            store: 技能存储（提供 list_active）。

        Returns:
            list[EvolutionContext]（每个是 FIX 类型）。
        """
        results: list[EvolutionContext] = []
        for rec in store.list_active():
            if not rec.enabled:
                continue
            # anti-loop: 新进化 skill selections=0 不触发
            if rec.total_selections < self._min_selections:
                continue
            # anti-loop: cooldown——自上次进化需 cooldown_turns 次新 selections
            last = self._last_evolve_selections.get(rec.name, 0)
            if rec.total_selections - last < self._cooldown_turns:
                continue

            evo_type, direction = self._diagnose_skill_health(rec)
            if evo_type is None:
                continue
            # 2a 只产 FIX；DERIVED 留 2b
            if evo_type != "FIX":
                continue

            # Phase 2 LLM 确认（llm None 跳过，纯规则）
            if self._llm is not None and not self._llm_confirm_evolution(rec, direction):
                continue

            results.append(EvolutionContext(
                trigger="METRIC",
                evolution_type="FIX",
                target_skill=rec,
                fix_direction=direction,
            ))
        return results

    def mark_evolved(self, skill_name: str, total_selections: int) -> None:
        """EvolutionManager 进化完成后调，记录 anti-loop 锚点。

        Args:
            skill_name:      被进化的 skill 名。
            total_selections: 进化时的 selections 数（作为下次 cooldown 的基准）。
        """
        self._last_evolve_selections[skill_name] = total_selections

    @staticmethod
    def _diagnose_skill_health(
        record: SkillRecord,
    ) -> tuple[str | None, str]:
        """规则诊断（Phase 1）。返 (evolution_type, direction)；健康返 (None, "")。

        判定顺序（短路）：
            1. fallback_rate > _FALLBACK_THRESHOLD
                 → ("FIX", "高 fallback：常选不用，指令不清或过时")
            2. applied_rate > _HIGH_APPLIED_FOR_FIX
               且 completion_rate < _LOW_COMPLETION_THRESHOLD
                 → ("FIX", "高 applied 低 completion：指令可能错或不全")
            3. effective_rate < _MODERATE_EFFECTIVE_THRESHOLD
               且 applied_rate > _MIN_APPLIED_FOR_DERIVED
                 → ("DERIVED", "中等效果，可派生增强版")
            4. 否则 → (None, "")

        阈值宽松——后续 LLM 确认步过滤误报。
        """
        # 高 fallback → 常选不用 → FIX
        if record.fallback_rate > _FALLBACK_THRESHOLD:
            return "FIX", (
                f"高 fallback_rate({record.fallback_rate:.0%})：skill 常被选但未应用，"
                f"指令不清或过时。"
            )
        # 用了但没完成 → 指令错 → FIX
        if (record.applied_rate > _HIGH_APPLIED_FOR_FIX
                and record.completion_rate < _LOW_COMPLETION_THRESHOLD):
            return "FIX", (
                f"低 completion_rate({record.completion_rate:.0%}) 但高 applied_rate"
                f"({record.applied_rate:.0%})：skill 指令可能错或不全。"
            )
        # 中等效果 → DERIVED（2b）
        if (record.effective_rate < _MODERATE_EFFECTIVE_THRESHOLD
                and record.applied_rate > _MIN_APPLIED_FOR_DERIVED):
            return "DERIVED", (
                f"中等 effective_rate({record.effective_rate:.0%})：可派生增强版。"
            )
        return None, ""

    def _llm_confirm_evolution(self, record: SkillRecord, direction: str) -> bool:
        """Phase 2 LLM 确认：问 LLM 这 skill 真需进化吗。

        构造 prompt（skill 名 / 描述 / 诊断 / metrics），
        调 llm.invoke([HumanMessage])，返回是否含 "yes"。
        异常 → 返 False（保守不触发）。

        Args:
            record:    待确认的 skill。
            direction: Phase 1 规则给出的诊断方向。

        Returns:
            True 表示确认需进化；False 表示不确认 / 异常。
        """
        try:
            prompt = (
                f"Skill: {record.name}\n描述: {record.description}\n"
                f"诊断: {direction}\n"
                f"metrics: selections={record.total_selections}, "
                f"effective_rate={record.effective_rate:.0%}, "
                f"fallback_rate={record.fallback_rate:.0%}\n"
                f"这 skill 真需要进化吗？只返 yes 或 no。"
            )
            from langchain_core.messages import HumanMessage
            resp = self._llm.invoke([HumanMessage(content=prompt)])
            content = resp.content if hasattr(resp, "content") else str(resp)
            return "yes" in content.lower()
        except Exception:
            return False  # LLM 失败保守不触发