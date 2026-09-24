"""Installer — install/uninstall/update 编排。

【整体职责】
hub 的安装编排器：把"下载 → 安全扫描 → 落盘注册 → 记录 provenance → 审计"
串成一条流程，并负责反向的 uninstall 与未来的 update。
- install   : source.fetch → SkillsGuard.scan → parser.install
              → HubLockFile.add → AuditLog.append。
- uninstall : HubLockFile.get → 删目录 → HubLockFile.remove → AuditLog.append。
- update    : 对比 upstream hash 与本地 content_hash（MVP 占位）。

设计（design_docs/46 §2.6）：
- source.fetch → SkillsGuard.scan → parser.install → HubLockFile.add → AuditLog.append。
- uninstall 反向流程。
- update 对比 upstream hash 与本地 content_hash。

【内容摘要】
- __init__(sources, lock_file, guard, audit_log, dest_root)
      注入 sources 字典 + 三件套（缺省用默认构造）+ 目标根目录。
- install(identifier, name=None) -> str        : 对外主入口，编排安装六步。
- uninstall(name) -> None                      : 反向流程。
- update(name=None) -> list[str]               : MVP 占位，返空。
- _resolve_source(identifier) -> SkillSource | None : 按前缀匹配 source。
- _derive_name(identifier) -> str              : 从 identifier 推导 name。
- _compute_hash(skill_dir) -> str              : 算 SKILL.md sha256。
- _now_iso() -> str                            : UTC ISO 时间戳。
- _derive_upstream_url(source, identifier) -> str | None : MVP 返 None。

【职责边界】
- 只负责：编排安装 / 卸载 / 更新流程，调 source / guard / parser / lock / audit。
- 不负责：搜索（unified_search）、具体 source 实现（sources/）、安全规则（SkillsGuard）、
  provenance 存储（HubLockFile）、审计格式（AuditLog）、技能解析（parser）。
- 不下载：下载由 source.fetch 负责，本类只调它。
- 不扫描：安全规则在 SkillsGuard，本类只调它。
- 不做 update 实现：MVP 占位，返空列表。

【INVARIANT】
- source 解析：按 identifier 前缀匹配 sources 字典的 key；无法解析 → ValueError。
- name 推导顺序：@ 后部分 → "/" 末段 → ":" 后部分 → 原 identifier。
- 安装六步固定顺序：
    1. source.fetch(identifier, dest_dir)。
    2. SkillsGuard.scan(skill_dir, source.name, identifier)。
       scan.allowed=False → 抛 ValueError（skill 已被隔离）。
    3. parser.install(skill_dir, name, dest_root) → skill_id。
    4. _compute_hash(skill_dir) → content_hash。
    5. HubLockFile.add(HubLockEntry)。
    6. AuditLog.append("install", ...)。
- uninstall：lock 无记录 → ValueError；目录存在 → rmtree；然后 remove + audit。
- update 为 MVP 占位：恒返 []。
- _compute_hash 无 SKILL.md → 返 ""。
- _derive_upstream_url 为 MVP 占位：恒返 None。
- dest_root 缺省 ~/.poirot/skills。
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from poirot.backend.agents.skill.hub.hub_store import (
    AuditLog,
    HubLockEntry,
    HubLockFile,
    ScanResult,
    SkillsGuard,
)
from poirot.backend.agents.skill.hub.source import SkillSource

logger = logging.getLogger(__name__)


class Installer:
    """install/uninstall/update 编排。

    流程：source.fetch → SkillsGuard.scan → parser.install → HubLockFile.add → AuditLog.append。

    构造参数：
    - sources   : dict[str, SkillSource]，key 是 identifier 前缀（如 "github"）。
    - lock_file : HubLockFile（缺省用默认路径）。
    - guard     : SkillsGuard（缺省用默认隔离目录）。
    - audit_log : AuditLog（缺省用默认日志路径）。
    - dest_root : 安装根目录（缺省 ~/.poirot/skills）。
    """

    def __init__(
        self,
        sources: dict[str, SkillSource],
        lock_file: HubLockFile | None = None,
        guard: SkillsGuard | None = None,
        audit_log: AuditLog | None = None,
        dest_root: Path | None = None,
    ) -> None:
        """保存依赖与目标根目录（三件套可缺省）。"""
        self._sources = sources
        self._lock_file = lock_file or HubLockFile()
        self._guard = guard or SkillsGuard()
        self._audit_log = audit_log or AuditLog()
        self._dest_root = dest_root or (Path.home() / ".poirot" / "skills")

    def install(
        self,
        identifier: str,
        name: str | None = None,
    ) -> str:
        """安装 skill（对外主入口）。

        步骤：
            1. _resolve_source(identifier) 解析 source；失败 → ValueError。
            2. name 为 None → _derive_name(identifier)；仍空 → ValueError。
            3. source.fetch(identifier, dest_dir) 下载。
            4. SkillsGuard.scan(skill_dir, source.name, identifier)：
                 allowed=False → logger.warning + 抛 ValueError（已隔离）。
            5. parser.install(skill_dir, name, dest_root) → skill_id。
            6. _compute_hash(skill_dir) → content_hash。
            7. HubLockFile.add(HubLockEntry)。
            8. AuditLog.append("install", ...)。

        Args:
            identifier: source 特定的唯一标识（如 github:owner/repo@skill-name）。
            name:       安装后的 skill 名；None 时从 identifier 推导。

        Returns:
            skill_id（parser.install 返回）。

        Raises:
            ValueError: source 无法解析 / name 无法推导 / SkillsGuard 拒绝。
        """
        source = self._resolve_source(identifier)
        if source is None:
            raise ValueError(f"Cannot resolve source for identifier: {identifier}")

        # 推导 name（@ 后部分，或 repo 名）
        if name is None:
            name = self._derive_name(identifier)
        if not name:
            raise ValueError(f"Cannot derive name from identifier: {identifier}")

        # 1. source.fetch → 临时目录
        dest_dir = self._dest_root / name
        skill_dir = source.fetch(identifier, dest_dir)

        # 2. SkillsGuard.scan
        scan_result = self._guard.scan(skill_dir, source.name, identifier)
        if not scan_result.allowed:
            logger.warning(
                "Skill %s rejected by SkillsGuard: %s (quarantined to %s)",
                name, scan_result.reasons, scan_result.quarantine_path,
            )
            raise ValueError(
                f"Skill {name} rejected: {scan_result.reasons}"
            )

        # 3. parser.install → 写入 SkillStore（复用既有 install 函数）
        from poirot.backend.agents.skill.parser import install as parser_install

        skill_id = parser_install(skill_dir, name, self._dest_root)

        # 4. 计算 content_hash
        content_hash = self._compute_hash(skill_dir)

        # 5. HubLockFile.add → 记录 provenance
        entry = HubLockEntry(
            name=name,
            source=source.name,
            identifier=identifier,
            install_path=str(self._dest_root / name),
            installed_at=self._now_iso(),
            content_hash=content_hash,
            upstream_url=self._derive_upstream_url(source, identifier),
        )
        self._lock_file.add(entry)

        # 6. AuditLog.append → 留痕
        self._audit_log.append(
            "install", name, source=source.name,
            identifier=identifier, skill_id=skill_id,
        )

        logger.info("Skill %s installed (id=%s, source=%s)", name, skill_id, source.name)
        return skill_id

    def uninstall(self, name: str) -> None:
        """卸载 skill：HubLockFile.get → 删目录 → HubLockFile.remove → AuditLog.append。

        Args:
            name: 技能名。

        Raises:
            ValueError: lock 文件中无此记录。
        """
        entry = self._lock_file.get(name)
        if entry is None:
            raise ValueError(f"Skill {name} not found in hub lock file")

        # 删目录
        skill_path = Path(entry.install_path)
        if skill_path.exists():
            import shutil

            shutil.rmtree(skill_path)

        # HubLockFile.remove
        self._lock_file.remove(name)

        # AuditLog.append
        self._audit_log.append("uninstall", name, source=entry.source)

        logger.info("Skill %s uninstalled", name)

    def update(self, name: str | None = None) -> list[str]:
        """检查更新（MVP：返空，需 upstream hash 对比实现）。

        name=None 时检查所有 hub skill。
        返有更新的 skill name 列表。

        Args:
            name: 技能名；None 表示检查全部。

        Returns:
            []（MVP）。
        """
        # MVP：不实现 update（需重新 fetch + hash 对比）
        # 进阶：对比 upstream_url hash 与本地 content_hash
        return []

    def _resolve_source(self, identifier: str) -> SkillSource | None:
        """从 identifier 前缀解析 source。

        匹配规则：identifier 以 "<prefix>:" 或 "<prefix>" 开头。
        支持的前缀：github / well-known / claude-marketplace / builtin。

        Args:
            identifier: 安装标识。

        Returns:
            SkillSource；无法解析返回 None。
        """
        for prefix, source in self._sources.items():
            if identifier.startswith(prefix + ":") or identifier.startswith(prefix):
                return source
        return None

    def _derive_name(self, identifier: str) -> str:
        """从 identifier 推导 skill name。

        顺序：
            1. "@" 后部分（github:owner/repo@skill-name → skill-name）。
            2. "/" 末段（github:owner/repo → repo）。
            3. ":" 后部分（builtin:name → name）。
            4. 原 identifier。

        Args:
            identifier: 安装标识。

        Returns:
            推导出的 name。
        """
        # github:owner/repo@skill-name → skill-name
        if "@" in identifier:
            return identifier.rsplit("@", 1)[1]
        # github:owner/repo → repo
        if "/" in identifier:
            return identifier.split("/")[-1]
        # builtin:name → name
        if ":" in identifier:
            return identifier.split(":", 1)[1]
        return identifier

    def _compute_hash(self, skill_dir: Path) -> str:
        """计算 SKILL.md 的 sha256（十六进制）。

        无 SKILL.md → 返 ""。

        Args:
            skill_dir: skill 目录。

        Returns:
            sha256 十六进制字符串或 ""。
        """
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            return ""
        return hashlib.sha256(skill_md.read_bytes()).hexdigest()

    def _now_iso(self) -> str:
        """返当前 UTC 时间的 ISO 字符串。"""
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).isoformat()

    def _derive_upstream_url(self, source: SkillSource, identifier: str) -> str | None:
        """推导 upstream URL（用于 update 检查）。

        MVP 占位：恒返 None（进阶实现）。

        Args:
            source:     source 实例。
            identifier: 安装标识。

        Returns:
            None（MVP）。
        """
        # MVP：返 None（进阶实现）
        return None