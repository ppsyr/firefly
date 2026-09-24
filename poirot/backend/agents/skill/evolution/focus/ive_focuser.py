"""IVEFocuser — IVE 5 问失败聚焦（借鉴 EvoSkills IVE）。

【整体职责】
evolution 闭环的第二环：聚焦失败证据，诊断出失败性质（fundamental / implementation）
与修复方向，增强 EvolutionContext 供 Mutator 消费。

IVE 5 问诊断区分 fundamental（skill 方向错）vs implementation（执行偏差）：
1. 失败发生在 skill 哪一步？
2. 是 skill 指令本身错（fundamental）还是执行偏差（implementation）？
3. 若 implementation，是工具调用失败 / 上下文不足 / 模型理解偏差？
4. 失败是否可经微调 skill 文本修复？
5. 修复方向是什么（不直接改，给 Mutator 输入）？

升级规则：implementation 累计 impl_fail_threshold 次（默认 3）升级 fundamental
（防保守分类导致无限重试，借鉴 EvoSkills）。

降级：LLM=None 时退化为全量摘要（不崩，弱但可用）。

【内容摘要】
- __init__(llm, impl_fail_threshold)                    : 保存依赖 + implementation 计数表。
- focus(ctx, store) -> EvolutionContext                 : 对外主入口。
- _degrade_focus(ctx, judgment_notes) -> EvolutionContext : LLM=None 降级路径。
- _read_judgment_notes(ctx, store) -> list[str]         : 读 SkillJudgment 历史补充证据。
- _llm_diagnose(ctx, skill_name, judgment_notes)       : LLM 5 问诊断。
    -> tuple[FailureClass, str]

【职责边界】
- 只负责：诊断失败性质 + 产 fix_direction + 增强 ctx.failure_evidence。
- 不负责：触发（triggers）、变异（mutator）、评估（eval_bridge）、门控（gate）、
  持久化（store 只读）、编排（EvolutionManager）。
- 不直接改 skill：只给 Mutator 输入（fix_direction）。

【INVARIANT】
- focus 输入 ctx.failure_evidence 为空（如 CAPTURED）→ 原样返回，不做诊断。
- LLM=None → 降级为全量摘要，默认 IMPLEMENTATION（保守，不轻易判 fundamental）。
- LLM 调用失败 → 保守返 IMPLEMENTATION（不轻易杀掉 skill）。
- 升级：implementation 累计 >= impl_fail_threshold → 升级 fundamental，附提示文本。
- fundamental → 重置该 skill 的 implementation 计数。
- D-L3-20：读 store 的 SkillJudgment 历史，deviation_note 作失败证据补充。
- 返回新 ctx（dataclasses.replace），不修改原对象（frozen）。
- LLM 返回的 class 非法值 → 回退 "IMPLEMENTATION"。
"""
from __future__ import annotations

from typing import Any

from poirot.backend.agents.skill.evolution.types import (
    EvolutionContext,
    FailureClass,
    FailureEvidence,
)


class IVEFocuser:
    """IVE 5 问诊断。LLM 调用产 failure_class + fix_direction。

    依赖：
    - llm                : 语言模型；None 时走降级路径。
    - impl_fail_threshold: implementation 累计升级 fundamental 的阈值（默认 3）。

    状态：
    - _impl_fail_counts  : skill_name → implementation 累计次数（跨调用持久）。
    """

    def __init__(
        self,
        llm: Any | None = None,
        impl_fail_threshold: int = 3,
    ) -> None:
        """保存依赖与阈值；初始化 implementation 计数表。"""
        self._llm = llm
        self._impl_fail_threshold = impl_fail_threshold
        # skill_name → implementation 累计次数
        self._impl_fail_counts: dict[str, int] = {}

    def focus(self, ctx: EvolutionContext, store: Any) -> EvolutionContext:
        """聚焦失败证据，产 failure_class + fix_direction（对外主入口）。

        步骤：
            1. ctx.failure_evidence 为空（如 CAPTURED）→ 原样返回。
            2. D-L3-20：读 SkillJudgment 历史补充失败证据。
            3. llm is None → _degrade_focus（全量摘要，默认 IMPLEMENTATION）。
            4. LLM 5 问诊断 → (failure_class, fix_direction)。
            5. implementation 累计升级：
                 IMPLEMENTATION → 计数 +1；达阈值 → 升级 FUNDAMENTAL + 提示文本。
                 FUNDAMENTAL    → 重置计数。
            6. 用 replace 更新 ctx（failure_evidence 的 failure_class + fix_direction）。

        Args:
            ctx:   EvolutionContext（trigger 产出）。
            store: 技能存储（读 judgments，可 None）。

        Returns:
            增强后的 EvolutionContext（新对象，frozen）。
        """
        if not ctx.failure_evidence:
            # CAPTURED 无失败证据，直接返原 ctx（CAPTURED 是沉淀非修复）
            return ctx

        # D-L3-20: 读 SkillJudgment 历史补充失败证据
        judgment_notes = self._read_judgment_notes(ctx, store)

        if self._llm is None:
            # 降级：全量摘要，默认 IMPLEMENTATION（保守，不轻易判 fundamental）
            return self._degrade_focus(ctx, judgment_notes)

        # LLM 5 问诊断
        skill_name = ctx.target_skill.name if ctx.target_skill else ctx.suggested_name
        failure_class, fix_direction = self._llm_diagnose(ctx, skill_name, judgment_notes)

        # implementation 累计升级 fundamental
        if failure_class == "IMPLEMENTATION":
            self._impl_fail_counts[skill_name] = self._impl_fail_counts.get(skill_name, 0) + 1
            if self._impl_fail_counts[skill_name] >= self._impl_fail_threshold:
                failure_class = "FUNDAMENTAL"
                fix_direction = (
                    f"[升级 fundamental] implementation 累计 "
                    f"{self._impl_fail_counts[skill_name]} 次，skill 方向可能有误。"
                    f" {fix_direction}"
                )
        elif failure_class == "FUNDAMENTAL":
            # fundamental 重置 implementation 计数
            self._impl_fail_counts[skill_name] = 0

        # 更新 failure_evidence 的 failure_class
        updated_evidence = tuple(
            FailureEvidence(
                turn_index=e.turn_index,
                tool_name=e.tool_name,
                failure_class=failure_class,
                description=e.description,
                impl_fail_count=self._impl_fail_counts.get(skill_name, 0),
            )
            for e in ctx.failure_evidence
        )
        from dataclasses import replace
        return replace(ctx, failure_evidence=updated_evidence, fix_direction=fix_direction)

    def _degrade_focus(
        self, ctx: EvolutionContext, judgment_notes: list[str] | None = None,
    ) -> EvolutionContext:
        """LLM=None 降级：全量摘要，默认 IMPLEMENTATION。

        把失败证据描述 + judgment 偏差拼成 fix_direction；
        不修改 failure_evidence 的 failure_class（保持原值）。

        Args:
            ctx:            EvolutionContext。
            judgment_notes: SkillJudgment 偏差记录。

        Returns:
            新的 EvolutionContext（仅 fix_direction 变化）。
        """
        summaries = [e.description for e in ctx.failure_evidence]
        parts = ["失败证据：" + " | ".join(summaries)]
        if judgment_notes:
            parts.append("SkillJudgment 偏差：" + " | ".join(judgment_notes))
        fix_direction = "LLM 未启用，降级全量摘要。" + " ".join(parts)
        from dataclasses import replace
        return replace(ctx, fix_direction=fix_direction)

    @staticmethod
    def _read_judgment_notes(ctx: EvolutionContext, store: Any) -> list[str]:
        """D-L3-20：从 store 读 SkillJudgment 历史，返 deviation_note 列表。

        过滤空 note；store 为 None / 无 target_skill / 读取异常 → 返 []。

        Args:
            ctx:   EvolutionContext。
            store: 技能存储。

        Returns:
            list[str]（deviation_note）。
        """
        if store is None or ctx.target_skill is None:
            return []
        try:
            judgments = store.get_judgments(ctx.target_skill.skill_id, limit=10)
            return [j.deviation_note for j in judgments if j.deviation_note]
        except Exception:
            return []

    def _llm_diagnose(
        self, ctx: EvolutionContext, skill_name: str,
        judgment_notes: list[str] | None = None,
    ) -> tuple[FailureClass, str]:
        """LLM 5 问诊断。返 (failure_class, fix_direction)。

        构造 prompt（skill 描述 + 失败证据 + judgment 偏差 + 5 问），
        调 llm.invoke([HumanMessage])，提取 JSON 中的 class / direction。
        异常或 JSON 缺失 → 保守返 ("IMPLEMENTATION", "")。
        非法 class → 回退 "IMPLEMENTATION"。

        Args:
            ctx:            EvolutionContext。
            skill_name:     skill 名（target 或 suggested）。
            judgment_notes: SkillJudgment 偏差记录。

        Returns:
            (FailureClass, fix_direction)。
        """
        try:
            import json
            from langchain_core.messages import HumanMessage

            skill_desc = ctx.target_skill.description if ctx.target_skill else ctx.capture_pattern
            evidence_text = "\n".join(
                f"- turn={e.turn_index} tool={e.tool_name}: {e.description}"
                for e in ctx.failure_evidence
            )
            judgment_text = ""
            if judgment_notes:
                judgment_text = "\n\nSkillJudgment 偏差记录:\n" + "\n".join(
                    f"- {n}" for n in judgment_notes
                )
            prompt = (
                f"Skill: {skill_name}\n描述: {skill_desc}\n\n"
                f"失败证据:\n{evidence_text}{judgment_text}\n\n"
                f"IVE 5 问诊断：\n"
                f"1. 失败发生在 skill 哪一步？\n"
                f"2. 是 skill 指令本身错（FUNDAMENTAL）还是执行偏差（IMPLEMENTATION）？\n"
                f"3. 若 implementation，子类（工具失败/上下文不足/模型理解偏差）？\n"
                f"4. 可否微调 skill 文本修复？\n"
                f"5. 修复方向？\n\n"
                f'只返 JSON: {{"class": "FUNDAMENTAL"|"IMPLEMENTATION", "direction": "修复方向"}}'
            )
            resp = self._llm.invoke([HumanMessage(content=prompt)])
            content = resp.content if hasattr(resp, "content") else str(resp)
            # 提取 JSON
            s = content.find("{")
            e_idx = content.rfind("}")
            if s != -1 and e_idx != -1 and e_idx > s:
                data = json.loads(content[s : e_idx + 1])
                cls = data.get("class", "IMPLEMENTATION")
                if cls not in ("FUNDAMENTAL", "IMPLEMENTATION"):
                    cls = "IMPLEMENTATION"
                return cls, data.get("direction", "")
            return "IMPLEMENTATION", ""
        except Exception:
            # LLM 失败保守返 IMPLEMENTATION
            return "IMPLEMENTATION", ""