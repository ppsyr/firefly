"""BuiltinSource — 扫 builtin_skills/ 树（零网络）。

【整体职责】
hub 的内置技能源：扫 builtin_skills/ 目录树，把内置技能转成 SkillMeta。
- 零网络：内置技能随包提交，全部在本地。
- 复用既有能力：调 SkillManager.search_builtin_skills 完成实际搜索。
- 已激活的技能标 is_installed=True。

设计（design_docs/46 §2.4）：
- 调既有 SkillManager.search_builtin_skills，转 SkillMeta。
- 已激活的 skill 标 is_installed=True。
- source="builtin"。

【内容摘要】
- name = "builtin"                                      : 源标识。
- __init__(skill_manager)                              : 保存 SkillManager（可 None）。
- search(query, limit) -> list[SkillMeta]              : 搜 builtin_skills/ 树。
- fetch(identifier, dest_dir) -> Path                  : 返 builtin skill 路径（不下载）。
- preview(identifier) -> str | None                    : 预览 SKILL.md 内容。

【职责边界】
- 只负责：扫本地 builtin_skills/ 树 + 转 SkillMeta。
- 不负责：搜索编排（search.py）、安装流程（installer.py）、中心存储（hub_store.py）、
  技能解析（parser）、持久化（store）。
- 不做网络访问：零网络。
- 不做下载：builtin skill 已在本地，fetch 直接返路径。

【INVARIANT】
- 实现 SkillSource Protocol 的 name / search / fetch / preview。
- source 固定为 "builtin"。
- identifier 格式：builtin:<name>。
- is_installed 取自 SkillManager.search_builtin_skills 返回的 is_active。
- SkillManager 延迟加载（避免循环 import）；mgr 为 None 时 search 返 []。
- fetch 不下载：直接返回 builtin skill 的 path（installer 应优先用 install_path）。
- preview 走 fetch 拿路径，读文件；文件不存在返 None。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from poirot.backend.agents.skill.hub.source import SkillMeta


class BuiltinSource:
    """扫 builtin_skills/ 树的 source（零网络）。

    调既有 SkillManager.search_builtin_skills，转 SkillMeta。

    构造参数：
    - skill_manager : SkillManager（可 None；None 时延迟构造）。
    """

    name = "builtin"

    def __init__(self, skill_manager: Any | None = None) -> None:
        """保存 SkillManager（可 None，延迟加载）。"""
        self._skill_manager = skill_manager

    def search(self, query: str, limit: int = 10) -> list[SkillMeta]:
        """搜 builtin_skills/ 树，返 SkillMeta 列表。

        步骤：
            1. mgr None → 延迟 build_skill_manager()（避免循环 import）。
            2. mgr 仍 None → 返 []。
            3. 调 mgr.search_builtin_skills(query)。
            4. 把每条结果转为 SkillMeta（source="builtin"，
               identifier=f"builtin:{name}"，is_installed 取自 is_active）。
            5. 取前 limit 条返回。

        Args:
            query: 关键词。
            limit: 最多返回条数。

        Returns:
            list[SkillMeta]。
        """
        mgr = self._skill_manager
        if mgr is None:
            # 延迟加载，避免循环 import
            from poirot.backend.agents.skill import build_skill_manager

            mgr = build_skill_manager()
        if mgr is None:
            return []

        builtin_results = mgr.search_builtin_skills(query)
        metas: list[SkillMeta] = []
        for r in builtin_results[:limit]:
            metas.append(SkillMeta(
                name=r.get("name", ""),
                description=r.get("description", ""),
                category=r.get("category", "core"),
                source="builtin",
                identifier=f"builtin:{r.get('name', '')}",
                install_path=r.get("path"),
                is_installed=r.get("is_active", False),
            ))
        return metas

    def fetch(self, identifier: str, dest_dir: Path) -> Path:
        """builtin skill 已在本地，直接返其 path。

        identifier 格式：builtin:<name>。

        注意：builtin skill 不需要下载，install_path 已在 search 时返回；
        caller（Installer）应优先用 install_path 而非 fetch。
        本方法仅作协议兼容：从 skill_manager 查出对应 builtin path。

        Args:
            identifier: 形如 "builtin:<name>"。
            dest_dir:   目标目录（builtin 场景未使用）。

        Returns:
            builtin skill 的 SKILL.md 路径；未找到返回 dest_dir。
        """
        # builtin skill 不需要下载，install_path 已在 search 时返回
        # caller (Installer) 应直接用 install_path 而非 fetch
        # 这里返 identifier 对应的 builtin path（从 skill_manager 查）
        mgr = self._skill_manager
        if mgr is None:
            from poirot.backend.agents.skill import build_skill_manager

            mgr = build_skill_manager()
        if mgr is None:
            return dest_dir

        # 解析 identifier: builtin:<name>
        name = identifier.split(":", 1)[1] if ":" in identifier else identifier
        # 搜 builtin 找 path
        results = mgr.search_builtin_skills(name)
        for r in results:
            if r.get("name") == name:
                return Path(r.get("path", "."))
        return dest_dir

    def preview(self, identifier: str) -> str | None:
        """预览 builtin SKILL.md 内容。

        走 fetch 拿路径，读文件；文件不存在返 None。

        Args:
            identifier: 形如 "builtin:<name>"。

        Returns:
            SKILL.md 文本；文件不存在返回 None。
        """
        path = self.fetch(identifier, Path("."))
        if path.exists():
            return path.read_text(encoding="utf-8")
        return None