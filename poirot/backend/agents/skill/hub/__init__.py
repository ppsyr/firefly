"""Skill Hub 模块 — 多 source skill 发现 + 安装 + 安全 + provenance。

【整体职责】
skill 的"输入侧"：从多个外部来源发现、安装、更新技能，并把关安全与留痕。
- 发现：跨 source 聚合搜索（builtin / github / well-known / claude-marketplace）。
- 安装：下载 → 安全扫描 → 落盘注册 → 记 provenance → 审计。
- 安全：SkillsGuard 在落盘前扫描 prompt injection / 敏感路径 / 可疑 URL。
- 留痕：HubLockFile 记来源，AuditLog 记每次操作。

设计（design_docs/46 §2）：
- SkillSource Protocol + 多 source adapter
  （Builtin / GitHub / WellKnown / ClaudeMarketplace）。
- HubLockFile 跟踪 provenance + SkillsGuard 安全扫描 + AuditLog 留痕。
- CLI + slash command 双入口。
- 零外部依赖：复用既有 agents/skill/ 模块（parser / store / selector）。

【内容摘要】
- source.py     : SkillSource Protocol + SkillMeta（发现抽象）。
- sources/      : 4 个源实现。
    - BuiltinSource            （零网络，扫 builtin_skills/）。
    - GitHubSource             （git clone）。
    - ClaudeMarketplaceSource  （拉 registry）。
    - WellKnownSource          （拉多 endpoint）。
- search.py     : unified_search / unified_search_as_dicts（跨源聚合）。
- installer.py  : Installer（install / uninstall / update 编排）。
- hub_store.py  : HubLockFile（provenance）+ SkillsGuard（安全）+ AuditLog（留痕）。

【职责边界】
- 只负责：发现、安装、卸载、更新、安全扫描、provenance、审计。
- 不负责：运行期的选择（selector）/ 注入（injector）/ 打点（middleware）/
  评估（eval）/ 进化（evolution）。
- 不做技能解析：复用 parser.parse_skill_file / parser.install。
- 不做持久化实现：复用 store（SQLiteSkillStore）。
- 下载由 source.fetch 负责，Installer 只编排，不自己下载。

【INVARIANT】
- 所有 source 实现 SkillSource Protocol（search / fetch / preview）。
- sources 字典的 key 是 identifier 前缀（builtin / github / well-known /
  claude-marketplace），Installer._resolve_source 靠它分发。
- 安装六步固定：fetch → scan → install → hash → lock.add → audit.append。
- SkillsGuard 在落盘前拦截：命中风险 → quarantine + 拒装。
- HubLockFile / AuditLog 落 ~/.poirot/skills/.hub/。
- 降级原则：搜索层 silent（尽量给结果）；安装层抛 ValueError（让用户知道）；
  文件层容错（读写不抛）。
- config 开关：hub_enabled / hub_quarantine_enabled / hub_audit_log。
- MVP 占位：GitHubSource.search、ClaudeMarketplaceSource.fetch、
  WellKnownSource.fetch、Installer.update。
"""
from __future__ import annotations