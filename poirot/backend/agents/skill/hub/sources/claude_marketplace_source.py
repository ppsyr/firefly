"""ClaudeMarketplaceSource — 拉 Claude Marketplace registry 安装 skill。

【整体职责】
hub 的远程技能源之一：从 Claude Marketplace registry（Anthropic 官方技能生态）
发现技能，转成 SkillMeta。
- 拉取 registry（HTTP GET + JSON 解析）。
- 按关键词匹配 name / description，转 SkillMeta。
- registry 不可达时降级返空（不抛异常）。

设计（design_docs/46 §2.4）：
- 拉 Claude Marketplace registry（Anthropic 官方 skill 生态）。
- 转 SkillMeta。
- identifier 格式：claude-marketplace:<skill-name>。

【内容摘要】
模块常量：
- _MARKETPLACE_REGISTRY_URL : Claude Marketplace registry URL（MVP 用 placeholder）。

类方法：
- __init__(registry_url)                : 保存 registry URL。
- search(query, limit) -> list[SkillMeta] : 搜 registry，转 SkillMeta。
- fetch(identifier, dest_dir) -> Path   : 下载 skill（MVP 占位）。
- preview(identifier) -> str | None     : 预览 SKILL.md（MVP 返 None）。
- _fetch_registry() -> list[dict] | None: HTTP GET registry，解析 JSON。

【职责边界】
- 只负责：拉 registry + 关键词匹配 + 转 SkillMeta。
- 不负责：搜索编排（search.py）、安装流程（installer.py）、中心存储（hub_store.py）、
  技能解析（parser）、持久化（store）。
- MVP 未实现 fetch / preview 的完整逻辑：fetch 只占位返 dest_dir，preview 恒返 None。
- 不做认证：MVP 无凭证处理（官方 registry 如公开则可直接 GET）。

【INVARIANT】
- 实现 SkillSource Protocol 的 name / search / fetch / preview。
- source 固定为 "claude-marketplace"。
- identifier 格式：claude-marketplace:<skill-name>。
- registry 不可达 / HTTP 非 200 / JSON 解析失败 → 降级返空（不抛异常）。
- registry 支持两种响应结构：list，或 dict 且含 "skills" 键。
- search 大小写不敏感，匹配 name 或 description。
- is_installed 恒 False（远程源不在本地，需安装后才为 True）。
- fetch 为 MVP 占位：直接返 dest_dir（完整 HTTP 下载留后续）。
- preview 为 MVP 占位：恒返 None（不支持预览）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from poirot.backend.agents.skill.hub.source import SkillMeta


# Claude Marketplace registry URL（Anthropic 官方，MVP 用 placeholder）
_MARKETPLACE_REGISTRY_URL = "https://registry.claude.com/skills/index.json"


class ClaudeMarketplaceSource:
    """从 Claude Marketplace 发现 skill 的 source。

    拉 Claude Marketplace registry，转 SkillMeta。
    registry 不可达时降级为返空（不抛异常）。

    构造参数：
    - registry_url : registry URL（默认 _MARKETPLACE_REGISTRY_URL）。
    """

    name = "claude-marketplace"

    def __init__(self, registry_url: str | None = None) -> None:
        """保存 registry URL（缺省用模块常量）。"""
        self._registry_url = registry_url or _MARKETPLACE_REGISTRY_URL

    def search(self, query: str, limit: int = 10) -> list[SkillMeta]:
        """搜索 Claude Marketplace，返匹配 SkillMeta 列表。

        步骤：
            1. _fetch_registry 拉 registry；失败 / 空 → 返 []。
            2. 大小写不敏感匹配 name 或 description。
            3. 命中 → 转 SkillMeta（source="claude-marketplace"，
               identifier=f"claude-marketplace:{name}"，is_installed=False）。
            4. 达 limit → 提前返回。

        Args:
            query: 关键词。
            limit: 最多返回条数。

        Returns:
            list[SkillMeta]；registry 不可达时返 []。
        """
        try:
            registry = self._fetch_registry()
            if not registry:
                return []
        except Exception:
            return []

        query_lower = query.lower()
        results: list[SkillMeta] = []
        for skill in registry:
            name = skill.get("name", "")
            desc = skill.get("description", "")
            if query_lower in name.lower() or query_lower in desc.lower():
                results.append(SkillMeta(
                    name=name,
                    description=desc,
                    category=skill.get("category", "claude"),
                    source="claude-marketplace",
                    identifier=f"claude-marketplace:{name}",
                    preview_url=skill.get("preview_url"),
                    is_installed=False,
                ))
                if len(results) >= limit:
                    return results
        return results

    def fetch(self, identifier: str, dest_dir: Path) -> Path:
        """下载 Claude Marketplace skill。

        MVP 占位：直接返 dest_dir；完整 HTTP 下载留后续实现。

        Args:
            identifier: 形如 "claude-marketplace:<skill-name>"。
            dest_dir:   目标目录。

        Returns:
            dest_dir（占位）。
        """
        return dest_dir

    def preview(self, identifier: str) -> str | None:
        """预览 Claude Marketplace SKILL.md。

        MVP 占位：恒返 None（不支持预览）。

        Args:
            identifier: 形如 "claude-marketplace:<skill-name>"。

        Returns:
            None。
        """
        return None

    def _fetch_registry(self) -> list[dict[str, Any]] | None:
        """HTTP GET Claude Marketplace registry，返 skill 列表。

        行为：
            - httpx.get(url, timeout=10, follow_redirects=True)。
            - status != 200 → None。
            - JSON 为 list → 直接返回。
            - JSON 为 dict 且含 "skills" → 返回 data["skills"]。
            - 其他情况或异常 → None。

        Returns:
            list[dict]；失败 / 不可达 / 结构不符 → None。
        """
        try:
            import httpx

            response = httpx.get(self._registry_url, timeout=10, follow_redirects=True)
            if response.status_code != 200:
                return None
            data = response.json()
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and "skills" in data:
                return data["skills"]
            return None
        except Exception:
            return None