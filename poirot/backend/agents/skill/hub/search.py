"""unified_search — 跨 source 聚合搜索。

【整体职责】
hub 的统一搜索入口：把多个 source（builtin + hub sources）的搜索结果聚合成一份。
- 聚合：遍历所有 source，收集 SkillMeta。
- 去重：按 name 去重（先到先得）。
- 排序：is_installed 优先，其次按 name 字典序。
- 截断：取前 limit 条。
- 降级：sources 为空 / builtin 不可用时，只搜 builtin 或返空。

设计（design_docs/46 §2.6）：
- 跨 source 聚合搜索（builtin + hub sources 并行）。
- 去重（按 name）+ 排序（is_installed 优先）+ limit 截断。
- hub 不可用时降级为只搜 builtin。

【内容摘要】
- unified_search(query, sources, limit) -> list[SkillMeta]
    聚合搜索主函数，返回 SkillMeta 列表。
- unified_search_as_dicts(query, sources, limit) -> list[dict]
    dict 版本（供 JSON 返回）。

【职责边界】
- 只负责：跨 source 聚合、去重、排序、截断、降级。
- 不负责：具体 source 实现（在 sources/ 下）、安装（installer.py）、
  provenance（HubLockFile）、安全扫描（SkillsGuard）、审计（AuditLog）。
- 不做并行：当前是顺序遍历 sources（不是并发）。
- 不做持久化：只返结果，不落库。

【INVARIANT】
- sources 为空 → 降级为 [BuiltinSource()]；builtin 不可用 → 返 []。
- 单个 source 异常 → 跳过该 source（logger.warning），继续其他。
- 去重按 name：空 name 跳过；同名只保留首次出现的。
- 排序 key：(not is_installed, name)——is_installed 优先，再按 name 升序。
- limit 截断在排序之后。
- 两个函数共享同一逻辑：as_dicts 只做字段投影。
- as_dicts 输出字段固定 8 个：name / description / category / source /
  identifier / is_installed / install_path / preview_url。
"""
from __future__ import annotations

import logging
from typing import Any

from poirot.backend.agents.skill.hub.source import SkillMeta, SkillSource

logger = logging.getLogger(__name__)


def unified_search(
    query: str,
    sources: list[SkillSource] | None = None,
    limit: int = 10,
) -> list[SkillMeta]:
    """跨 source 聚合搜索（对外主入口）。

    步骤：
        1. sources 为空 → 降级为 [BuiltinSource()]；builtin 不可用 → 返 []。
        2. 顺序遍历 sources：
             - source.search(query, limit=limit)。
             - 按 name 去重（空 name 跳过）。
             - source 异常 → warning + 跳过。
        3. 排序：(not is_installed, name)——is_installed 优先。
        4. limit 截断。

    Args:
        query:   关键词。
        sources: source 列表；None / 空时降级为 builtin。
        limit:   最多返回条数。

    Returns:
        list[SkillMeta]。
    """
    if not sources:
        # 降级：只搜 builtin
        try:
            from poirot.backend.agents.skill.hub.sources.builtin_source import (
                BuiltinSource,
            )

            sources = [BuiltinSource()]
        except Exception as e:
            logger.warning("unified_search: builtin source unavailable: %s", e)
            return []

    all_results: list[SkillMeta] = []
    seen_names: set[str] = set()

    for source in sources:
        try:
            results = source.search(query, limit=limit)
            for meta in results:
                if meta.name and meta.name not in seen_names:
                    seen_names.add(meta.name)
                    all_results.append(meta)
        except Exception as e:
            logger.warning(
                "unified_search: source %s failed: %s",
                getattr(source, "name", "unknown"), e,
            )
            continue

    # 排序：is_installed 优先
    all_results.sort(key=lambda m: (not m.is_installed, m.name))

    # limit 截断
    return all_results[:limit]


def unified_search_as_dicts(
    query: str,
    sources: list[SkillSource] | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """unified_search 的 dict 版本（供 JSON 返回）。

    输出字段固定 8 个：
    name / description / category / source / identifier /
    is_installed / install_path / preview_url。

    Args:
        query:   关键词。
        sources: source 列表；None / 空时降级为 builtin。
        limit:   最多返回条数。

    Returns:
        list[dict]。
    """
    metas = unified_search(query, sources, limit)
    return [
        {
            "name": m.name,
            "description": m.description,
            "category": m.category,
            "source": m.source,
            "identifier": m.identifier,
            "is_installed": m.is_installed,
            "install_path": m.install_path,
            "preview_url": m.preview_url,
        }
        for m in metas
    ]