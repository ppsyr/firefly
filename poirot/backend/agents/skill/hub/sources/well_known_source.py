"""WellKnownSource — HTTP GET /.well-known/skills/index.json 安装 skill。

【整体职责】
hub 的远程技能源之一：从任意网站的 /.well-known/skills/index.json 发现技能。
- 任何网站可暴露 skill 索引（约定路径 /.well-known/skills/index.json）。
- HTTP GET index.json，关键词匹配 name / description。
- endpoint 不可达时降级为返空（不抛异常）。

设计（design_docs/46 §2.4）：
- 任何网站可暴露 skill 索引（/.well-known/skills/index.json）。
- HTTP GET index.json，关键词匹配 name/description。
- endpoint 不可达时降级为返空（不抛异常）。
- identifier 格式：well-known:https://example.com 或 well-known:example.com。

【内容摘要】
类方法：
- __init__(endpoints)                       : 保存 well-known endpoint 列表。
- search(query, limit) -> list[SkillMeta]   : 遍历 endpoints，拉 index 匹配。
- fetch(identifier, dest_dir) -> Path       : 下载 skill（MVP 占位）。
- preview(identifier) -> str | None         : 预览 SKILL.md（MVP 返 None）。
- _fetch_index(endpoint) -> list[dict] | None : HTTP GET index.json。

【职责边界】
- 只负责：拉各 endpoint 的 index.json + 关键词匹配 + 转 SkillMeta。
- 不负责：搜索编排（search.py）、安装流程（installer.py）、中心存储（hub_store.py）、
  技能解析（parser）、持久化（store）。
- MVP 未实现 fetch / preview：fetch 只占位返 dest_dir，preview 恒返 None。

【INVARIANT】
- 实现 SkillSource Protocol 的 name / search / fetch / preview。
- source 固定为 "well-known"。
- identifier 格式：well-known:<endpoint>@<skill-name>。
- endpoints 缺省为空列表；用户配置补充。
- index URL 形如：{endpoint}/.well-known/skills/index.json（endpoint 去尾斜杠）。
- index 支持两种响应结构：list，或 dict 且含 "skills" 键。
- endpoint 不可达 / HTTP 非 200 / JSON 解析失败 / 结构不符 → 降级跳过（不抛异常）。
- search 大小写不敏感，匹配 name 或 description；跨 endpoint 累积，达 limit 提前返回。
- is_installed 恒 False（远程源不在本地）。
- fetch / preview 为 MVP 占位。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from poirot.backend.agents.skill.hub.source import SkillMeta


class WellKnownSource:
    """从 /.well-known/skills/index.json 发现 skill 的 source。

    任何网站可暴露 skill 索引。HTTP GET index.json，关键词匹配。
    endpoint 不可达时降级为返空（不抛异常）。

    构造参数：
    - endpoints : well-known URL 列表（如 ["https://example.com"]）。
                  None 时用默认（空列表，用户配置补充）。
    """

    name = "well-known"

    def __init__(self, endpoints: list[str] | None = None) -> None:
        """保存 well-known endpoints（缺省为空列表）。

        Args:
            endpoints: well-known URL 列表；None 表示无默认 endpoint。
        """
        self._endpoints = endpoints or []

    def search(self, query: str, limit: int = 10) -> list[SkillMeta]:
        """搜索 well-known endpoints，返匹配 SkillMeta 列表（对外主入口）。

        步骤：
            1. 对每个 endpoint：
                 a. _fetch_index 拉 index；失败 / 空 → 跳过该 endpoint。
                 b. 大小写不敏感匹配 name / description。
                 c. 命中 → 转 SkillMeta（source="well-known"，
                    identifier=f"well-known:{endpoint}@{name}"，is_installed=False）。
                 d. 达 limit → 提前返回。
            2. endpoint 异常 → 跳过（降级）。

        Args:
            query: 关键词。
            limit: 最多返回条数。

        Returns:
            list[SkillMeta]；无命中 / 全部 endpoint 不可达时返 []。
        """
        results: list[SkillMeta] = []
        query_lower = query.lower()

        for endpoint in self._endpoints:
            try:
                index = self._fetch_index(endpoint)
                if not index:
                    continue
                for skill in index:
                    name = skill.get("name", "")
                    desc = skill.get("description", "")
                    if query_lower in name.lower() or query_lower in desc.lower():
                        results.append(SkillMeta(
                            name=name,
                            description=desc,
                            category=skill.get("category", "unknown"),
                            source="well-known",
                            identifier=f"well-known:{endpoint}@{name}",
                            preview_url=skill.get("preview_url"),
                            is_installed=False,
                        ))
                        if len(results) >= limit:
                            return results
            except Exception:
                # endpoint 不可达，降级为跳过
                continue

        return results

    def fetch(self, identifier: str, dest_dir: Path) -> Path:
        """下载 skill 到 dest_dir。

        MVP 占位：直接返 dest_dir（需 HTTP GET 下载 SKILL.md 内容）。
        进阶：HTTP GET endpoint + skill_name → 下载 SKILL.md + 相关文件。

        identifier 格式：well-known:<endpoint>@<skill-name>

        Args:
            identifier: 形如 "well-known:<endpoint>@<skill-name>"。
            dest_dir:   目标目录。

        Returns:
            dest_dir（占位）。
        """
        # MVP：不实现 HTTP 下载（需 httpx GET SKILL.md content）
        # 进阶：HTTP GET endpoint + skill_name → 下载 SKILL.md + 相关文件
        return dest_dir

    def preview(self, identifier: str) -> str | None:
        """预览 well-known SKILL.md。

        MVP 占位：恒返 None（需 HTTP GET）。

        Args:
            identifier: 形如 "well-known:<endpoint>@<skill-name>"。

        Returns:
            None（MVP）。
        """
        return None

    def _fetch_index(self, endpoint: str) -> list[dict[str, Any]] | None:
        """HTTP GET {endpoint}/.well-known/skills/index.json，返 skill 列表。

        行为：
            - URL = f"{endpoint.rstrip('/')}/.well-known/skills/index.json"。
            - httpx.get(url, timeout=10, follow_redirects=True)。
            - status != 200 → None。
            - JSON 为 list → 直接返回。
            - JSON 为 dict 且含 "skills" → 返回 data["skills"]。
            - 其他情况 / 异常 → None（不抛异常）。

        Args:
            endpoint: 站点根 URL（如 "https://example.com"）。

        Returns:
            list[dict]；失败 / 不可达 / 结构不符 → None。
        """
        url = f"{endpoint.rstrip('/')}/.well-known/skills/index.json"
        try:
            import httpx

            response = httpx.get(url, timeout=10, follow_redirects=True)
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