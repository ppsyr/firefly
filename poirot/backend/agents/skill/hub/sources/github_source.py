"""GitHubSource — git clone owner/repo 安装 skill。

【整体职责】
hub 的远程技能源之一：通过 git clone 从 GitHub 安装 skill。
- 搜索：MVP 未实现（需 GitHub Search API + token）。
- 获取：git clone --depth 1 owner/repo 到 dest_dir。
- 预览：MVP 未实现（需 GitHub Contents API + token）。
- 缓存：search 结果按 repo URL 缓存 1 小时（hermes 模式）。

设计（design_docs/46 §2.4）：
- git clone --depth 1 owner/repo 到 ~/.poirot/skills/<name>/。
- 支持 GITHUB_TOKEN env var 提升限速。
- cache index 1 小时（hermes 模式）。
- identifier 格式：github:owner/repo@skill-name 或 github:owner/repo。

【内容摘要】
模块常量：
- _search_cache : dict[str, tuple[float, list[SkillMeta]]]，repo URL → (时间戳, 结果)。
- _CACHE_TTL    : 缓存有效期（3600 秒 = 1 小时）。

类方法：
- search(query, limit) -> list[SkillMeta]           : MVP 返空。
- fetch(identifier, dest_dir) -> Path               : git clone + 返回 skill 路径。
- preview(identifier) -> str | None                 : MVP 返 None。
- _parse_identifier(identifier) -> (owner, repo, skill_name) : 解析 identifier。
- _build_clone_url(owner, repo) -> str              : 构造 clone URL（可带 token）。
- _get_cached(repo_url) -> list[SkillMeta] | None   : 读 cache（TTL 1h）。
- _set_cached(repo_url, results)                    : 写 cache。

【职责边界】
- 只负责：git clone 安装 + identifier 解析 + clone URL 构造 + 缓存。
- 不负责：搜索编排（search.py）、安装后处理（installer 负责解析 + 落库）、
  中心存储（hub_store.py）、技能解析（parser）、持久化（store）。
- MVP 未实现 search / preview：search 恒返空，preview 恒返 None。
- 不做 GitHub API 调用：fetch 用 git 命令而非 API。

【INVARIANT】
- 实现 SkillSource Protocol 的 name / search / fetch / preview。
- source 固定为 "github"。
- identifier 支持四种格式：
    github:owner/repo
    github:owner/repo@skill-name
    owner/repo
    owner/repo@skill-name
- identifier 解析后 owner / repo 必须存在（否则 ValueError）。
- git clone 用 --depth 1（浅克隆）；check=True（失败抛异常）。
- GITHUB_TOKEN 存在时 clone URL 形如 https://<token>@github.com/owner/repo.git。
- fetch：若 identifier 带 skill_name 且 dest_dir/skill_name 存在 → 返该子目录；
  否则返 dest_dir。
- 缓存 TTL 3600 秒；过期视为未命中。
- search / preview 为 MVP 占位（返空 / None）。
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from poirot.backend.agents.skill.hub.source import SkillMeta


# cache：repo URL → (timestamp, SkillMeta list)
_search_cache: dict[str, tuple[float, list[SkillMeta]]] = {}
_CACHE_TTL = 3600  # 1 hour


class GitHubSource:
    """从 GitHub repo 安装 skill 的 source（git clone）。

    支持 GITHUB_TOKEN env var 提升限速。
    cache index 1 小时（hermes 模式）。
    """

    name = "github"

    def search(self, query: str, limit: int = 10) -> list[SkillMeta]:
        """搜索 GitHub skill（MVP：返空，需 GitHub API 集成）。

        MVP 不实现 GitHub API 搜索（需 GitHub Search API + token）。
        用户通过 identifier 直接 install：github:owner/repo@skill-name。
        进阶：实现 GitHub Search API 搜 SKILL.md 文件。

        Args:
            query: 关键词（MVP 未使用）。
            limit: 最多返回条数（MVP 未使用）。

        Returns:
            []（MVP）。
        """
        # MVP：不实现搜索，返空（用户用 identifier 直接 install）
        return []

    def fetch(self, identifier: str, dest_dir: Path) -> Path:
        """git clone owner/repo 到 dest_dir，返 skill 目录路径。

        步骤：
            1. _parse_identifier 解析出 (owner, repo, skill_name)。
            2. _build_clone_url 构造 clone URL（可带 GITHUB_TOKEN）。
            3. mkdir -p dest_dir。
            4. git clone --depth 1 <url> <dest_dir>（check=True）。
            5. 若 skill_name 存在且 dest_dir/skill_name 存在 → 返该子目录；
               否则返 dest_dir。

        Args:
            identifier: 形如 github:owner/repo@skill-name 或 github:owner/repo。
            dest_dir:   克隆目标目录。

        Returns:
            skill 目录路径。

        Raises:
            ValueError:    identifier 格式非法（缺 owner / repo）。
            subprocess.CalledProcessError: git clone 失败。
        """
        owner, repo, skill_name = self._parse_identifier(identifier)

        clone_url = self._build_clone_url(owner, repo)
        dest_dir.mkdir(parents=True, exist_ok=True)

        # git clone --depth 1
        subprocess.run(
            ["git", "clone", "--depth", "1", clone_url, str(dest_dir)],
            capture_output=True,
            text=True,
            check=True,
        )

        if skill_name:
            skill_path = dest_dir / skill_name
            if skill_path.exists():
                return skill_path
        return dest_dir

    def preview(self, identifier: str) -> str | None:
        """预览 GitHub SKILL.md（MVP：返 None，需 GitHub Contents API）。

        MVP 不实现预览（需 GitHub Contents API + token）。
        进阶：HTTP GET GitHub raw content。

        Args:
            identifier: 形如 github:owner/repo@skill-name。

        Returns:
            None（MVP）。
        """
        return None

    def _parse_identifier(self, identifier: str) -> tuple[str, str, str | None]:
        """解析 identifier：github:owner/repo@skill-name → (owner, repo, skill_name)。

        支持格式：
        - github:owner/repo
        - github:owner/repo@skill-name
        - owner/repo（无 github: 前缀）
        - owner/repo@skill-name

        Args:
            identifier: 待解析的标识符。

        Returns:
            (owner, repo, skill_name)；skill_name 可为 None。

        Raises:
            ValueError: identifier 不含 owner/repo（部分少于 2）。
        """
        # 去 github: 前缀
        if identifier.startswith("github:"):
            identifier = identifier[7:]

        # 分离 @skill-name
        skill_name: str | None = None
        if "@" in identifier:
            identifier, skill_name = identifier.rsplit("@", 1)

        # 分离 owner/repo
        parts = identifier.split("/")
        if len(parts) < 2:
            raise ValueError(
                f"Invalid github identifier: {identifier}. "
                "Expected: github:owner/repo@skill-name or owner/repo"
            )
        return parts[0], parts[1], skill_name

    def _build_clone_url(self, owner: str, repo: str) -> str:
        """构造 git clone URL（支持 GITHUB_TOKEN 提升限速）。

        GITHUB_TOKEN 存在 → https://<token>@github.com/owner/repo.git；
        否则 → https://github.com/owner/repo.git。

        Args:
            owner: 仓库 owner。
            repo:  仓库名。

        Returns:
            clone URL。
        """
        token = os.getenv("GITHUB_TOKEN")
        if token:
            return f"https://{token}@github.com/{owner}/{repo}.git"
        return f"https://github.com/{owner}/{repo}.git"

    def _get_cached(self, repo_url: str) -> list[SkillMeta] | None:
        """从 cache 读（TTL 1h）。

        命中且未过期 → 返缓存结果；否则返 None。

        Args:
            repo_url: 仓库 URL（cache key）。

        Returns:
            list[SkillMeta] 或 None。
        """
        if repo_url in _search_cache:
            ts, results = _search_cache[repo_url]
            if time.time() - ts < _CACHE_TTL:
                return results
        return None

    def _set_cached(self, repo_url: str, results: list[SkillMeta]) -> None:
        """写 cache（记录当前时间戳 + 结果）。

        Args:
            repo_url: 仓库 URL（cache key）。
            results:  搜索结果列表。
        """
        _search_cache[repo_url] = (time.time(), results)