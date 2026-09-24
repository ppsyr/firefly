"""skill_search 工具 —— 供 agent graph（ReAct）主动调用的技能搜索工具。

【整体职责】
让 agent 在接到任务时能「主动」搜索技能，而不是只能依赖 /skill search 命令。
把 builtin 技能搜索与 hub 技能搜索合并，返回统一的 JSON 列表，
帮助 agent 在动手前先确认「是否已有现成技能可用」。

【组成】
1. 唯一对外工具：skill_search(query, limit=5)
   - 内部先取 SkillManager，再分别搜 builtin 与 hub，合并去重后返回 JSON。
2. 数据来源：
   - builtin：SkillManager.search_builtin_skills（零网络，扫 builtin_skills/ 树）。
   - hub：skill.hub.search.unified_search（需 hub 启用；不可用时降级为只搜 builtin）。

【返回格式】
JSON 列表，元素字段：
    name / description / category / source / is_active / path
（注：hub 结果为 SkillMeta，会先转成 dict 再统一字段。）

【分组归属】
归 core group —— default 与 expert 两种模式都加载（见 agent_tools 的分组白名单）。

【职责边界】
- 本工具只做「搜索 + 合并 + 返回」，不负责技能的加载、激活或安装。
- hub 不可用时降级为「只搜 builtin」，不抛异常、不影响 builtin 结果。
- 结果按 name 去重并截断到 limit。

【设计意图（find-skills 主动触发的核心）】
agent 接到 specialized task 时，先调此工具 check 是否有相关 skill，
再决定是否直接复用已有技能。
"""
from __future__ import annotations

import json
import logging

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


@tool("skill_search")
def skill_search(query: str, limit: int = 5) -> str:
    """Search builtin + hub skills by keyword. Returns JSON list of matches.

    Call this PROACTIVELY when:
    - User asks for a specialized task (frontend design, chart, github PR, debug, tdd, etc.)
    - You're about to start a complex task that might benefit from existing skill
    - Before writing code for a common pattern (check if skill exists first)

    Returns: [{"name", "description", "category", "source", "is_active", "path"}]

    Common trigger keywords:
    - frontend / UI / React / Vue → creative/frontend-design
    - chart / graph / visualization → creative/chart-visualization
    - diagram / architecture → creative/architecture-diagram
    - github / PR / code review → software-development/github-*
    - debug / bug → core/systematic-debugging
    - test / TDD → core/test-driven-development
    - plan / spike → core/plan / core/spike
    - simplify / refactor → core/simplify-code
    """
    from poirot.backend.agents.skill import build_skill_manager

    # 取 SkillManager（技能系统的统一入口）；不可用则返回空列表
    mgr = build_skill_manager()
    if mgr is None:
        return "[]"

    # builtin 搜索（零网络，扫 builtin_skills/ 树）
    builtin_results = mgr.search_builtin_skills(query)

    # hub 搜索（如果 hub 启用；不可用时降级为只搜 builtin）
    hub_results: list[dict] = []
    try:
        from poirot.backend.agents.skill.hub.search import unified_search

        hub_results = unified_search(query, limit=limit)
    except ImportError:
        # hub 模块未安装（add-skill-hub change 未实现），降级为只搜 builtin
        pass
    except Exception as e:
        logger.warning("skill_search hub search failed, degrading to builtin only: %s", e)

    # 合并 + 去重（按 name）+ 截断
    seen_names: set[str] = set()
    all_results: list[dict] = []
    for r in builtin_results + hub_results:
        # 统一转 dict（builtin 返 dict，hub 返 SkillMeta frozen dataclass）
        if hasattr(r, "name"):
            # SkillMeta → dict
            r_dict = {
                "name": r.name,
                "description": r.description,
                "category": r.category,
                "source": r.source,
                "identifier": r.identifier,
                "is_installed": r.is_installed,
                "install_path": r.install_path,
                "preview_url": r.preview_url,
            }
        else:
            # 已是 dict
            r_dict = r
        name = r_dict.get("name", "")
        if name and name not in seen_names:
            seen_names.add(name)
            # 统一字段格式（builtin 已有 source 缺失，补 "builtin"）
            if "source" not in r_dict:
                r_dict["source"] = "builtin"
            all_results.append(r_dict)

    return json.dumps(all_results[:limit], ensure_ascii=False, indent=2)