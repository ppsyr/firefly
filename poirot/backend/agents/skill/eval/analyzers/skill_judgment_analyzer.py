"""SkillJudgmentAnalyzer — 执行层 eval（OpenSpace ExecutionAnalyzer 模型）。

【整体职责】
执行层评估器：任务结束后，用一次 LLM 调用分析执行过程，
对每个被注入的 skill 判断"agent 是否实际应用了它"，并顺带产出进化建议。
- 产出：list[SkillJudgment] + list[EvolutionSuggestion]。
- 持久化：写 judgment、更新 L1 4 计数器（analyzer 内调 store.record_outcome）。
- 触发：异步 fire-and-forget（caller 负责 asyncio.create_task）。

【内容摘要】
模块常量：
- _MAX_MESSAGES_CHARS : 送入 LLM 的消息摘要字符上限（80000）。
- _MAX_JOURNAL_EVENTS : 送入 LLM 的 journal 事件条数上限（50）。

类方法：
- __init__(llm, store)                : 保存依赖（均可为 None）。
- analyze_execution(...)              : 对外主入口（async）。
- _llm_analyze(...)                   : 调 LLM + 解析 JSON → judgments + suggestions。
- _persist(task_id, judgments, ...)   : 写 judgment + 更新 L1 计数器。
- _format_events(events)              : journal 事件格式化为文本。
- _extract_json(content)              : 从文本提取首个 JSON 对象。

【职责边界】
- 只负责：执行层 LLM 判断 + 进化建议产出 + 持久化 judgment + 更新计数器。
- 不负责：任务层评分（TaskQualityJudge）、响应层检查（ResponseContractChecker）、
  趋势追踪（RuntimeTracker）、选择/注入（selector / injector）。
- 不负责调度：异步由 caller 创建 task（fire-and-forget），本类不自行起协程。
- 不决定是否进化：只产 EvolutionSuggestion，由 L2 trigger 决定。

【INVARIANT】
- D-L3-21：一次 LLM 调用同时产出 SkillJudgment + EvolutionSuggestion。
- D-L3-3 ：更新 L1 4 计数器（analyzer 内调 store.record_outcome）。
- D-L3-19：异步 fire-and-forget，caller 负责 asyncio.create_task。
- D-L3-14：无 skill 注入时返空（caller 负责跳过）。
- graceful degradation：LLM=None 或任何异常 → 返空列表，不崩。
- 只保留 skill_id 在 injected_skills 里的 judgment（过滤 LLM 幻觉）。
- evolution_type 非法值回退为 "FIX"。
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from poirot.backend.agents.journal.events import utc_now_iso
from poirot.backend.agents.skill.eval.types import (
    EvolutionSuggestion,
    SkillJudgment,
)

_MAX_MESSAGES_CHARS = 80000
_MAX_JOURNAL_EVENTS = 50


class SkillJudgmentAnalyzer:
    """执行层 eval：post-execution LLM 分析，产 per-skill SkillJudgment + EvolutionSuggestion。

    依赖：
    - llm   : 语言模型；None 时 analyze_execution 直接返空。
    - store : 持久化；None 时跳过持久化与计数器更新。
    """

    def __init__(self, llm: Any | None = None, store: Any = None) -> None:
        """保存依赖。

        Args:
            llm:   语言模型（可 None）。
            store: 技能存储（可 None，提供 save_judgment / record_outcome）。
        """
        self._llm = llm
        self._store = store

    async def analyze_execution(
        self,
        task_id: str,
        journal_events: list[dict],
        messages_summary: str,
        injected_skills: list[dict],
        task_completed: bool = True,
    ) -> tuple[list[SkillJudgment], list[EvolutionSuggestion]]:
        """LLM 分析执行日志，产 SkillJudgment + EvolutionSuggestion（对外主入口）。

        步骤：
            1. 前置检查：injected_skills 空 或 llm is None → 返 ([], [])。
            2. 调 _llm_analyze（LLM + JSON 解析）；异常 → 返 ([], [])。
            3. _persist 持久化 judgment + 更新 L1 计数器。
            4. 返回 (judgments, suggestions)。

        Args:
            task_id:         任务 id（也作 run_id 传给 record_outcome）。
            journal_events:  运行日志事件列表。
            messages_summary: 消息摘要（超长截断到 _MAX_MESSAGES_CHARS）。
            injected_skills: 本轮注入的 skill 清单（每项含 skill_id / name / description）。
            task_completed:  任务是否完成（传给 record_outcome）。

        Returns:
            (judgments, suggestions)；LLM 不可用或异常时返回 ([], [])。
        """
        if not injected_skills or self._llm is None:
            return [], []

        try:
            judgments, suggestions = self._llm_analyze(
                task_id, journal_events, messages_summary, injected_skills,
            )
        except Exception:
            return [], []

        self._persist(task_id, judgments, task_completed)
        return judgments, suggestions

    def _llm_analyze(
        self,
        task_id: str,
        journal_events: list[dict],
        messages_summary: str,
        injected_skills: list[dict],
    ) -> tuple[list[SkillJudgment], list[EvolutionSuggestion]]:
        """调 LLM 分析，解析 JSON 返 judgments + suggestions。

        行为：
        - 构造 prompt：skill 清单 + 摘要 + journal 事件。
        - 调 llm.invoke([HumanMessage]).
        - _extract_json 解析；失败 → ([], [])。
        - 过滤：只保留 skill_id 在 injected_skills 里的 judgment（防幻觉）。
        - evolution_type 非法值回退 "FIX"。

        Returns:
            (judgments, suggestions)。
        """
        from langchain_core.messages import HumanMessage

        skills_text = "\n".join(
            f"{i+1}. {s['skill_id']}: {s['name']} — {s.get('description', '')}"
            for i, s in enumerate(injected_skills)
        )
        events_text = self._format_events(journal_events)
        summary = (messages_summary or "")[:_MAX_MESSAGES_CHARS]

        prompt = (
            "你在分析一个已完成的 agent 任务执行。\n\n"
            f"被注入的 skills:\n{skills_text}\n\n"
            f"执行摘要:\n{summary}\n\n"
            f"journal 事件:\n{events_text}\n\n"
            "对每个被注入的 skill 判断：\n"
            "1. agent 是否实际应用了该 skill 的指导？（true/false）\n"
            "2. 有什么偏差？（简短记录）\n\n"
            "如有进化建议：\n"
            "- type: FIX（修复现有 skill）/ DERIVED（派生增强）/ CAPTURED（捕获新模式）\n"
            "- target: skill_id 列表\n"
            "- direction: 该修什么/该捕获什么\n\n"
            '只返 JSON: {"judgments": [{"skill_id": "...", "skill_applied": true, '
            '"deviation_note": "..."}], "suggestions": [{"evolution_type": "FIX", '
            '"target_skill_ids": ["..."], "direction": "..."}]}'
        )
        resp = self._llm.invoke([HumanMessage(content=prompt)])
        content = resp.content if hasattr(resp, "content") else str(resp)

        data = self._extract_json(content)
        if data is None:
            return [], []

        skill_map = {s["skill_id"]: s for s in injected_skills}
        judgments: list[SkillJudgment] = []
        for j_data in data.get("judgments", []):
            sid = j_data.get("skill_id", "")
            if sid not in skill_map:
                continue
            judgments.append(SkillJudgment(
                judgment_id=f"judgment_{uuid.uuid4().hex[:12]}",
                skill_id=sid,
                skill_name=skill_map[sid].get("name", ""),
                task_id=task_id,
                skill_applied=bool(j_data.get("skill_applied", False)),
                deviation_note=j_data.get("deviation_note", ""),
                timestamp=utc_now_iso(),
            ))

        suggestions: list[EvolutionSuggestion] = []
        for s_data in data.get("suggestions", []):
            etype = s_data.get("evolution_type", "FIX")
            if etype not in ("FIX", "DERIVED", "CAPTURED"):
                etype = "FIX"
            suggestions.append(EvolutionSuggestion(
                evolution_type=etype,  # type: ignore[arg-type]
                target_skill_ids=tuple(s_data.get("target_skill_ids", [])),
                direction=s_data.get("direction", ""),
            ))

        return judgments, suggestions

    def _persist(
        self, task_id: str, judgments: list[SkillJudgment], task_completed: bool,
    ) -> None:
        """持久化 judgment + 更新 L1 计数器。

        - store 为 None → 直接返回。
        - 每条 judgment：save_judgment + record_outcome（更新 4 计数器）。
        - 任一步异常 → silent（pass），不阻断其他 judgment。
        """
        if self._store is None:
            return
        for j in judgments:
            try:
                self._store.save_judgment(j)
                self._store.record_outcome(
                    j.skill_id, run_id=task_id,
                    applied=j.skill_applied,
                    task_completed=task_completed,
                    note=j.deviation_note,
                )
            except Exception:
                pass  # silent degradation

    @staticmethod
    def _format_events(events: list[dict]) -> str:
        """把 journal 事件格式化为文本（最多 _MAX_JOURNAL_EVENTS 条，每条截断 200 字符）。

        无事件时返回 "(无事件)"。
        """
        lines = []
        for e in (events or [])[:_MAX_JOURNAL_EVENTS]:
            etype = e.get("event_type", e.get("type", ""))
            lines.append(f"- {etype}: {json.dumps(e, ensure_ascii=False)[:200]}")
        return "\n".join(lines) or "(无事件)"

    @staticmethod
    def _extract_json(content: str) -> dict | None:
        """从文本提取首个 JSON 对象（'{' 到末个 '}'）。

        找不到或解析失败 → None。
        """
        s = content.find("{")
        e_idx = content.rfind("}")
        if s != -1 and e_idx != -1 and e_idx > s:
            try:
                return json.loads(content[s : e_idx + 1])
            except Exception:
                return None
        return None