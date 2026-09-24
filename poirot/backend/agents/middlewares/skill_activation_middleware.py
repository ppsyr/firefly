"""SkillActivationMiddleware — before_model 主动建议相关 skill。

【整体职责】
在每次进入 model 之前，分析最后一条 user message 的关键词，主动给出
「本次任务可能相关的 skill」建议，写入 ThreadState.skill_suggestion
（旁路存档）。lead agent 看到建议后，自己决定是否进一步调 skill_search
或读 SKILL.md —— 建议是提示性的，不强制。

【内容摘要】
- _KEYWORD_MAP                         : 关键词 → 候选 skill name 映射表。
- SkillActivationMiddleware.before_model: 从最后一条 user message 提取关键词并产出建议。
- SkillActivationMiddleware._match_keywords : 关键词匹配 → 去重后的建议列表。

【职责边界】
- 只负责：关键词匹配、产出建议、写 ThreadState.skill_suggestion（旁路字段）。
- 不负责：实际的 skill 选择与注入（SkillInjectionMiddleware）、skill 的存储
  与检索（skill 子系统）、agent 是否采纳建议（agent 自行决定）、
  skill_search 的执行（由 agent 调工具触发）。

【设计约定（设计文档 46 §3.2 改造点 D）】
- 用 before_model hook 做关键词分析并产出建议。
- 建议写进 ThreadState.skill_suggestion，形态为 list[dict]，
  每项含 keyword + skill name（旁路存档，不影响主对话流）。
- 不修改 system prompt（保护 prompt caching，INVARIANT §7.2.1）。
  因此建议不作为 SystemMessage 注入，只放在 state 字段里等 agent 自己取。
- 简单关键词匹配，零 LLM 调用（INVARIANT §7.2.3），保证低成本、无副作用。
- lead agent 看到建议后自行决定是否调 skill_search
  （建议非强制，INVARIANT §7.2.2）。

【INVARIANT】
- 只处理纯文本 content（非 str 形态如 list 则跳过）。
- 无消息 / 无命中 → 返回 None（不写 state）。
- 不修改 messages、不注入 SystemMessage，只写旁路字段 skill_suggestion。
- 去重：同一 skill 只出现一次（用 seen_skills 保证）。
- 零 LLM 调用（纯关键词匹配）。
- 与 SkillInjectionMiddleware 解耦：本中间件只建议，是否被采纳由 agent 决定。
"""
from __future__ import annotations

from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langgraph.runtime import Runtime


# ---------------------------------------------------------------------------
# 关键词 → 候选 skill name 映射
#
# 与 find-skills SKILL.md 的 keyword 表保持一致，作为本中间件的匹配依据。
# 结构：{关键词: [候选 skill name, ...]}
# ---------------------------------------------------------------------------
_KEYWORD_MAP: dict[str, list[str]] = {
    "frontend": ["frontend-design"],
    "ui": ["frontend-design"],
    "react": ["frontend-design"],
    "vue": ["frontend-design"],
    "chart": ["chart-visualization"],
    "graph": ["chart-visualization"],
    "visualization": ["chart-visualization"],
    "diagram": ["architecture-diagram", "concept-diagrams"],
    "github": ["github-code-review", "github-pr-workflow", "github-issues"],
    "pr": ["github-pr-workflow", "github-code-review"],
    "code review": ["github-code-review"],
    "debug": ["systematic-debugging", "python-debugpy"],
    "bug": ["systematic-debugging", "python-debugpy"],
    "test": ["test-driven-development"],
    "tdd": ["test-driven-development"],
    "plan": ["plan", "spike"],
    "spike": ["spike", "plan"],
    "refactor": ["simplify-code"],
    "simplify": ["simplify-code"],
}


class SkillActivationMiddleware(AgentMiddleware):
    """before_model：分析 user message 关键词，主动建议相关 skill。

    写 ThreadState.skill_suggestion（旁路存档），不修改 system prompt。
    lead agent 看到建议后自己决定是否调 skill_search / 读 SKILL.md。
    """

    def before_model(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        """同步 before_model hook：从最后一条 user message 提取关键词并产出建议。

        处理流程：
        1. 取 state.messages；为空则返回 None（无内容可分析）。
        2. 取最后一条消息的 content；
           若非 str（如 list 形态的多段内容）则返回 None（本 hook 只处理纯文本）。
        3. 把 content 转小写后交给 _match_keywords 做关键词匹配。
        4. 若无任何命中则返回 None（不写 state）。
        5. 否则返回 {"skill_suggestion": [...]} 作为 state patch，
           写入 ThreadState.skill_suggestion。

        注意：本 hook 不修改 messages、不注入 SystemMessage，只写旁路字段。

        Args:
            state: 当前 state，读取 messages。
            runtime: LangGraph 运行时（本 hook 未使用）。

        Returns:
            dict[str, Any] | None: 含 skill_suggestion 的 state patch；
                无建议时返回 None。
        """
        messages = state.get("messages") or []
        if not messages:
            return None

        last_msg = messages[-1]
        content = getattr(last_msg, "content", "")
        if not isinstance(content, str):
            return None

        suggestions = self._match_keywords(content.lower())
        if not suggestions:
            return None

        return {"skill_suggestion": suggestions}

    def _match_keywords(self, text: str) -> list[dict]:
        """关键词匹配 → 候选 skill name 列表（去重）。

        遍历 _KEYWORD_MAP，对每个 keyword 做子串包含判断；命中则把其
        候选 skill 逐个加入结果。用 seen_skills 保证同一个 skill 只出现一次。

        返回形态：
            [{"keyword": "frontend", "skill": "frontend-design"}, ...]

        Args:
            text: 已转小写的待匹配文本。

        Returns:
            list[dict]: 去重后的建议列表；无命中时返回空列表。
        """
        suggestions: list[dict] = []
        seen_skills: set[str] = set()
        for keyword, skill_names in _KEYWORD_MAP.items():
            if keyword in text:
                for skill_name in skill_names:
                    if skill_name not in seen_skills:
                        seen_skills.add(skill_name)
                        suggestions.append({"keyword": keyword, "skill": skill_name})
        return suggestions