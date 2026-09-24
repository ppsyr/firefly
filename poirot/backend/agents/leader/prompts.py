"""Leader 提示词模板 — 按 expert_mode / specialist / skills 条件拼装 system prompt。

【整体职责】
从 PromptManager 加载各段 .md 提示词，按 expert_mode 与启用状态条件拼装为 Leader 的
system prompt。expert 模式追加深度研究策略段；specialist 启用时注入 routing 段；
skills 启用时注入 Skill-First Principle 段。条件注入均以"空则不注入"保护 prompt caching。

【内容摘要】
- _build_specialist_routing_section : 条件构造 <specialist_routing> 段。
- apply_prompt_template             : 主入口，按条件拼装 system prompt。

【职责边界】
- 只负责：加载提示词段、按条件拼装 system prompt。
- 不负责：提示词文件的内容（prompts/system/leader/*.md）、提示词的加载与渲染机制
  （prompts/manager）、specialist / skill 的实现（multiagent / skill 模块）。

【INVARIANT】
- 拼装顺序固定：identity → constraints → decision_guidance →（expert）mode_expert
  →（条件）specialist_routing →（条件）skill_first_principle。
- 条件注入保护 caching：specialist_routing 仅在 specialist_registry 非空且有 specialist 时注入；
  skill_first_principle 仅在 skills_enabled 时注入；不满足则不注入（system prompt 不变）。
- 基础三段必含：identity / constraints / decision_guidance 始终拼装。
- mode 枚举已废弃：改用 expert_mode: bool 参数化。
- 异常容错：specialist 列表获取失败视为空，不注入 routing 段。
"""
from __future__ import annotations

from typing import Any

from poirot.backend.agents.prompts import get_prompt_manager


def _build_specialist_routing_section(specialist_registry: Any | None) -> str:
    """条件性注入 <specialist_routing> 段（Bug B 修复，设计文档 46 §4.2）。

    specialist_registry 非空（含至少一个 specialist）时返 routing 段，
    空时返空串（保护 prompt caching：不修改既有 system prompt）。

    段内容含 specialist 列表 + Delegation Principles（按任务类型路由：
    coding → specialist，研究/并行 → subagent，简单 Q&A → 自己做）。

    Args:
        specialist_registry: specialist 注册表，可选。

    Returns:
        str: routing 段文本；无 specialist 或获取失败时返回空串。
    """
    if specialist_registry is None:
        return ""
    try:
        specialists = specialist_registry.list_specialists()
    except Exception:
        return ""
    if not specialists:
        return ""

    # 构造 specialist 行（每个 specialist 一行，含 delegate_to_<name> tool 名）
    # 注意：SpecialistAgent Protocol 无 description 属性，用 name 推导描述
    specialist_lines = [f"- delegate_to_{name}: delegate coding task to {name} specialist" for name in specialists]
    specialist_lines.append("- delegate_to_subagent: isolated context + parallel subtasks (Poirot self-copy)")

    return f"""
<specialist_routing>
You have the following specialist agents available for delegation:

{chr(10).join(specialist_lines)}

## Delegation Principles（按任务类型路由）

- **coding tasks** (write/modify/test code) → delegate to coding specialist, don't write yourself
- **research / multi-source gathering / reasoning** → do it yourself (core capability)
- **parallel subtasks of same type** → delegate_to_subagent (isolated context, batch)
- **simple Q&A** → answer directly
- **task decomposition** → use delegate_to_subagent for parallel isolated context

## When NOT to delegate

- Task is simple (single file edit, quick lookup) → do it yourself
- No specialist available (all disabled) → do it yourself
- Specialist already failed once → retry with different context or do it yourself
</specialist_routing>
"""


def apply_prompt_template(
    expert_mode: bool = False,
    *,
    specialist_registry: Any | None = None,
    skills_enabled: bool = False,
    **context: Any,
) -> str:
    """Render the system prompt based on expert_mode flag.

    mode 枚举废弃后改为 expert_mode: bool 参数化：
    - default (expert_mode=False): identity + constraints + decision_guidance
    - expert  (expert_mode=True):  identity + constraints + decision_guidance + mode_expert

    从 PromptManager 加载各段 .md 文件拼装。

    Sections (in order):
    - <identity>            : agent role and capabilities
    - <constraints>         : language, content, format, scope rules
    - <decision_guidance>   : default 模式引导模型自判深度
    - <mode_expert>         : expert 模式追加深度研究策略（仅 expert_mode=True）
    - <specialist_routing>  : specialist 启用时条件注入（Bug B 修复，仅 specialist_registry 非空）
    - <skill_first_principle>: skills 启用时条件注入（F2 改造，仅 skills_enabled=True）

    Args:
        expert_mode: 是否专家模式。
        specialist_registry: SpecialistRegistry | None。非空时条件注入 routing 段
            （保护 prompt caching：空时不注入，system prompt 不变）。
        skills_enabled: bool。True 时条件注入 Skill-First Principle 段
            （保护 prompt caching：False 时不注入）。
        **context: 预留上下文参数（当前未使用）。

    Returns:
        str: 拼装完成的 system prompt。
    """
    pm = get_prompt_manager()
    parts = [
        pm.load("leader", "identity"),
        pm.load("leader", "constraints"),
        pm.load("leader", "decision_guidance"),
    ]
    if expert_mode:
        parts.append(pm.load("leader", "mode_expert"))
    # Bug B 修复：条件注入 specialist routing 段
    routing_section = _build_specialist_routing_section(specialist_registry)
    if routing_section:
        parts.append(routing_section)
    # F2 改造：条件注入 Skill-First Principle 段（skills 启用时）
    if skills_enabled:
        parts.append(pm.load("leader", "skill_first_principle"))
    return "\n\n".join(parts)