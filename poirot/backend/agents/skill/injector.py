"""Skill 注入文本构建 — markdown block，从 SKILL.md 文件读 body（去 frontmatter）。

【整体职责】
把选中的 active skills 拼成一段 markdown 注入块，供 SkillInjectionMiddleware
写入上下文。正文（body）从 SKILL.md 文件现读，不依赖 DB 里存的内容。

【内容摘要】
- build_injection_text(skills) : 构建注入用的 markdown 块（模块主函数）。
- _read_body(path)             : 读 SKILL.md 并剥离 frontmatter，返回正文。

【职责边界】
- 只负责：把 SkillRecord 列表渲染成 markdown、从文件读取并剥离 frontmatter。
- 不负责：技能选择（selector）、注入时机（middleware）、打点（metrics）、持久化（store）。
- 不持有状态：纯函数，每次调用现读文件。

【INVARIANT】
- 内容/索引分离：SkillRecord 只存 path，body 从文件读（source of truth 在文件）。
- frontmatter 剥离：`---\\n{yaml}\\n---\\n{body}` → 取 body。
- 文件读失败返空 body（不抛，注入 header 即可）。
"""
from __future__ import annotations

from pathlib import Path

from poirot.backend.agents.skill.types import SkillRecord


def build_injection_text(skills: list[SkillRecord]) -> str:
    """构建 active skills 的 markdown 注入块。

    输出结构（每个 skill 一段）：
        # Active Skills
        ### Skill: {name}
        **Path**: {path}
        {body}
        ---

    Args:
        skills: 本轮选中的 SkillRecord 列表。

    Returns:
        markdown 字符串；skills 为空时返回空串。
    """
    if not skills:
        return ""
    lines: list[str] = ["# Active Skills", ""]
    for rec in skills:
        body = _read_body(rec.path)
        lines.append(f"### Skill: {rec.name}")
        lines.append(f"**Path**: {rec.path}")
        lines.append("")
        lines.append(body.strip())
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)


def _read_body(path: str) -> str:
    """读 SKILL.md 并剥离 frontmatter，返回正文 body。

    剥离规则：文件以 `---` 开头且能切出三段（`---` / yaml / body）时取第三段，
    并去掉前导换行；否则原样返回全文。
    读文件失败（文件不存在、权限等）→ 返回空串，不抛异常。

    Args:
        path: SKILL.md 文件路径。

    Returns:
        正文 body；读失败返回 ""。
    """
    try:
        content = Path(path).read_text(encoding="utf-8")
    except Exception:
        return ""
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            return parts[2].lstrip("\r\n")
    return content