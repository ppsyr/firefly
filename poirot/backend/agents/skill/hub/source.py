"""SkillSource Protocol + SkillMeta — skill 发现抽象。

【整体职责】
定义 hub（技能中心）的"技能源"抽象与元数据契约：
- SkillSource : 每种 source 的 adapter 接口（search / fetch / preview）。
- SkillMeta   : source 返回的轻量元数据（搜索结果的统一格式）。

设计目标（design_docs/46 §2.3）：
- 每种 source（builtin / github / well-known / claude-marketplace）写一个 adapter。
- 新增 source = 新增 adapter + 注册，不动核心 search / install 逻辑。

【内容摘要】
- SkillMeta(frozen)      : source 返回的 skill 元数据。
- SkillSource(Protocol)  : source adapter 接口。
    - name                       : 源名（类属性）。
    - search(query, limit)       : 搜索 skill，返 list[SkillMeta]。
    - fetch(identifier, dest_dir): 下载/克隆到 dest_dir，返 skill 目录路径。
    - preview(identifier)        : 预览 SKILL.md 内容（不安装），可选。

【职责边界】
- 只负责：定义 source 抽象 + 元数据契约。
- 不负责：具体 source 实现（在 sources/ 下）、搜索编排（search.py）、
  安装流程（installer.py）、中心存储（hub_store.py）。
- 不做持久化：SkillMeta 是轻量元数据，不落库；已安装状态由 store 管理。
- 不管理版本/指标：不含 metrics / version lineage（install 后由 SkillStore 管理）。

【INVARIANT】
- SkillMeta 为 frozen（不可变值对象）。
- SkillMeta 与 SkillRecord 的区别：
    SkillMeta 是 source 搜索结果的轻量元数据（不含 metrics / version lineage）；
    SkillRecord 是 install 后由 SkillStore 管理的完整记录。
- SkillSource 的 identifier 是 source 特定的唯一标识（如 github:owner/repo@skill-name），
  安装时传给 installer。
- preview 是可选能力：不支持预览的 source 返 None。
- 新增 source 只需新增 adapter + 注册，不改核心 search / install 逻辑。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class SkillMeta:
    """source 返回的 skill 元数据。

    与 SkillRecord 的区别：SkillMeta 是 source 搜索结果的轻量元数据，
    不含 metrics / version lineage（这些在 install 后由 SkillStore 管理）。

    字段：
    - name          : 技能名。
    - description   : 技能描述。
    - category      : 类别（core / research / ...）。
    - source        : 来源标识："builtin" / "github" / "well-known" / "claude-marketplace"。
    - identifier    : 安装时传给 installer 的唯一标识。
    - install_path  : 已安装时的路径（未安装 None）。
    - preview_url   : SKILL.md 预览 URL（远程 source）。
    - is_installed  : 是否已安装。
    """

    name: str
    description: str
    category: str
    source: str                              # "builtin" / "github" / "well-known" / "claude-marketplace"
    identifier: str                          # 安装时传给 installer 的唯一标识
    install_path: str | None = None          # 已安装时的路径（未安装 None）
    preview_url: str | None = None           # SKILL.md 预览 URL（远程 source）
    is_installed: bool = False


class SkillSource(Protocol):
    """Skill registry source adapter。

    每种 source（builtin / github / well-known / claude-marketplace）写一个 adapter。
    新增 source = 新增 adapter + 注册，不动核心 search / install 逻辑。

    实现示例：BuiltinSource / GitHubSource / WellKnownSource / ClaudeMarketplaceSource。
    """

    name: str

    def search(self, query: str, limit: int = 10) -> list[SkillMeta]:
        """搜索 skill，返匹配的 SkillMeta 列表。

        Args:
            query: 关键词（匹配 name 或 description）。
            limit: 最多返回条数。

        Returns:
            list[SkillMeta]。
        """
        ...

    def fetch(self, identifier: str, dest_dir: Path) -> Path:
        """下载 / 克隆 skill 到 dest_dir，返 skill 目录路径。

        Args:
            identifier: source 特定的唯一标识（如 github:owner/repo@skill-name）。
            dest_dir:   目标目录（由 installer 创建）。

        Returns:
            skill 目录路径。
        """
        ...

    def preview(self, identifier: str) -> str | None:
        """预览 SKILL.md 内容（不安装）。

        Args:
            identifier: source 特定的唯一标识。

        Returns:
            SKILL.md 文本内容；source 不支持预览时返回 None。
        """
        ...