"""Skill 模块入口（门面层）。

【整体职责】
Skill 子系统的对外门面与装配入口。
对外暴露公共 API，并聚合 skill 的 store + selector + 注入/打点中间件，
供 bootstrap 统一构造与调用。同时决定内置技能目录的加载策略。

【模块内容】
公共 API（__all__）：
- SkillConfig                ：skill 配置对象。
- load_skill_config          ：从 .env 读取并构造 SkillConfig。
- SkillStore                 ：技能存储抽象接口。
- SQLiteSkillStore           ：技能存储的 SQLite 实现（记录 / 指标 / version DAG）。
- SkillSelector              ：技能选择器（从候选技能中挑选要注入的技能）。
- SkillManager               ：门面类（本模块主类）。
- build_skill_manager        ：工厂函数，从配置构造 SkillManager。

SkillManager.__init__ 的字段（模块核心）：
- _config                    ：SkillConfig，配置（构造时传入）。
- _store                     ：SQLiteSkillStore，唯一在 __init__ 中构造的依赖。
- _selector                  ：SkillSelector，置 None，由 load_startup 建。
- _injection                 ：注入中间件，置 None，由 load_startup 建。
- _metrics                   ：打点中间件，置 None，由 load_startup 建。
- _evolution                 ：EvolutionManager，置 None，由 bootstrap 回注。
- _eval_layer                ：L3 EvalLayer，置 None，由 bootstrap 回注。

SkillManager 其余成员：
- load_startup / get_injection_middleware / get_metrics_middleware
- get_evolution_manager / set_evolution_manager
- get_eval_layer / set_eval_layer
- list_skills / search_builtin_skills
- store / config（属性）

模块常量：
- _BUILTIN_SKILLS_DIR        ：内置技能根目录（包相对路径）。
- _BUILTIN_CORE_SKILLS_DIR   ：内置 core 子目录（启动时唯一自动 discover）。

【职责边界】
- 只负责：门面构造、启动装配、回注占位、查询展示、内置目录策略。
- 不负责：技能的解析 / 选择 / 注入 / 打点 / 进化 / 评估逻辑
  （分别在 parser / selector / injector / 中间件 / evolution / eval 下）。
- 不 import 中间件（lazy import 在方法体内），避免 skill → middlewares → skill 循环。

【INVARIANT】
- bootstrap 构造一次，生命周期与 AppRuntime 一致。
- 未 load_startup 时 _selector / _injection / _metrics 为 None。
- switch_expert_mode 不重建实例（store 持久）。
- EvolutionManager / EvalLayer 由 bootstrap 回注（避免循环依赖）。
- 内置目录用包相对路径，不依赖 cwd。
- 仅 core/ 子目录启动时自动 discover；其余子目录需 /skill search 或
  find-skills 按需搜索后使用。

【补充：主链位置】
bootstrap（build_skill_manager）
    → SkillManager.load_startup（装配 selector + 中间件）
    → SkillInjectionMiddleware（注入选中技能）
    → SkillMetricsMiddleware（打点）
    → SkillSelector（决策）
    → SQLiteSkillStore（持久化：记录 / 指标 / version DAG）
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from poirot.backend.agents.skill.config import SkillConfig, load_skill_config
from poirot.backend.agents.skill.selector import SkillSelector
from poirot.backend.agents.skill.store import SQLiteSkillStore, SkillStore

__all__ = [
    "SkillConfig",
    "SkillStore",
    "SQLiteSkillStore",
    "SkillSelector",
    "SkillManager",
    "build_skill_manager",
]

# 内置技能根目录（随包提交，已校验）。与用户技能目录（skills/，gitignore）机制相同，
# 都可通过 frontmatter enabled:false 或 store 禁用。使用包相对路径，不依赖 cwd。
#
# 加载策略：
# - 仅 core/ 子目录在启动时自动 discover（元技能，自动加载）；
# - 其余子目录（research / software-development / creative / productivity）
#   不自动加载，需通过 /skill search 命令或 find-skills 技能按需搜索后使用。
_BUILTIN_SKILLS_DIR = Path(__file__).parent / "builtin_skills"
_BUILTIN_CORE_SKILLS_DIR = _BUILTIN_SKILLS_DIR / "core"


class SkillManager:
    """Skill 子系统门面。

    聚合 store + selector + 注入/打点中间件，供外部统一访问。
    构造分两段：__init__ 轻装配（仅 store），load_startup 重装配（selector + middleware）。

    INVARIANT:
    - bootstrap 构造一次，生命周期与 AppRuntime 一致；
    - load_startup(llm) 完成 discover skill_dirs + sync_from_files + 建 selector/middleware；
    - get_injection_middleware / get_metrics_middleware 供中间件链注入；
    - switch_expert_mode 不重建实例（store 持久）；
    - 中间件类 lazy import（避免 skill → middlewares → skill 循环）。
    """

    def __init__(self, config: SkillConfig) -> None:
        """轻装配：仅建 store，其余字段置 None 待 load_startup / bootstrap 填充。

        组装规则：
            1. 保存 config（供后续读取 max_inject / quality_threshold 等）。
            2. 用 config.db_path 构造 SQLiteSkillStore。
            3. _selector / _injection / _metrics 置 None（load_startup 时建）。
            4. _evolution / _eval_layer 置 None（bootstrap 回注）。
        """
        self._config = config
        self._store = SQLiteSkillStore(config.db_path)
        self._selector: SkillSelector | None = None
        self._injection: Any = None
        self._metrics: Any = None
        self._evolution: Any = None  # EvolutionManager，由 bootstrap 装配（避免循环依赖）
        self._eval_layer: Any = None  # L3 EvalLayer，由 bootstrap 装配

    def get_evolution_manager(self) -> Any:
        """返回 EvolutionManager（未装配时 None）。"""
        return self._evolution

    def set_evolution_manager(self, manager: Any) -> None:
        """bootstrap 装配 EvolutionManager 后回注。

        为何回注：EvolutionManager 位于 skill.evolution，反向依赖 skill 基础层，
        在 __init__ 内构造会形成 skill → evolution → skill 循环，故由外层装配后注入。
        """
        self._evolution = manager

    def get_eval_layer(self) -> Any:
        """返回 L3 EvalLayer（未装配时 None）。"""
        return self._eval_layer

    def set_eval_layer(self, eval_layer: Any) -> None:
        """bootstrap 装配 L3 EvalLayer 后回注（原因同 set_evolution_manager）。"""
        self._eval_layer = eval_layer

    def load_startup(self, llm: Any | None = None) -> None:
        """启动期重装配：discover + sync + 建 selector 与中间件。

        主链装配的核心步骤：把文件系统里的技能同步进 store，并建好决策与执行组件。

        Args:
            llm: 语言模型对象，可选（None 时 selector 退化工作）。

        Returns:
            None。

        Raises:
            不主动抛异常；discover / register 的异常被吞掉（单条技能失败不阻断装配）。

        组装规则：
            1. discover 用户技能目录（origin=IMPORTED）→ register。
            2. 若 include_builtin 且 core 目录存在：
               discover 内置 core 目录（origin=BUILTIN）→ register。
            3. 构建 SkillSelector（store, llm, max_skills, quality_threshold,
               min_selections）。
            4. lazy import 两个中间件类（避免 skill → middlewares → skill 循环）。
            5. 构建 SkillInjectionMiddleware(store, selector)
               与 SkillMetricsMiddleware(store)。
        """
        user_dirs = [Path(d) for d in self._config.skill_dirs]
        for rec in self._store.discover(user_dirs, origin="IMPORTED"):
            try:
                self._store.register(rec)
            except Exception:
                pass
        if self._config.include_builtin and _BUILTIN_CORE_SKILLS_DIR.exists():
            for rec in self._store.discover([_BUILTIN_CORE_SKILLS_DIR], origin="BUILTIN"):
                try:
                    self._store.register(rec)
                except Exception:
                    pass

        self._selector = SkillSelector(
            self._store, llm,
            max_skills=self._config.max_inject,
            quality_threshold=self._config.quality_threshold,
            min_selections=self._config.min_selections,
        )

        # lazy import 避免循环依赖（middlewares 反向 import skill.injector / _ctx）
        from poirot.backend.agents.middlewares.skill_injection_middleware import (
            SkillInjectionMiddleware,
        )
        from poirot.backend.agents.middlewares.skill_metrics_middleware import (
            SkillMetricsMiddleware,
        )
        self._injection = SkillInjectionMiddleware(self._store, self._selector)
        self._metrics = SkillMetricsMiddleware(self._store)

    def get_injection_middleware(self) -> Any:
        """返回技能注入中间件（未 load_startup 时为 None）。"""
        return self._injection

    def get_metrics_middleware(self) -> Any:
        """返回技能打点中间件（未 load_startup 时为 None）。"""
        return self._metrics

    @property
    def store(self) -> SkillStore:
        """底层技能存储。"""
        return self._store

    @property
    def config(self) -> SkillConfig:
        """暴露 config，供 bootstrap 读取 evolve 等参数。"""
        return self._config

    def list_skills(self) -> list[dict]:
        """返回当前 active 技能概览，供 TUI / CLI 展示。

        Returns:
            list[dict]：每项含 skill_id / name / description /
            effective_rate（保留 3 位小数）/ total_selections / allowed_tools。
        """
        result: list[dict] = []
        for rec in self._store.list_active():
            result.append({
                "skill_id": rec.skill_id,
                "name": rec.name,
                "description": rec.description,
                "effective_rate": round(rec.effective_rate, 3),
                "total_selections": rec.total_selections,
                "allowed_tools": list(rec.allowed_tools),
            })
        return result

    def search_builtin_skills(self, query: str) -> list[dict]:
        """按关键词搜索全部 builtin_skills/（含未加载的非 core 技能）。

        与 list_skills 的区别：
        - core/ 下的技能启动时已 discover 为 active；
        - 其余子目录的技能仅存在于文件系统，不进 DB。

        本方法遍历整棵目录树，匹配 SKILL.md 的 name / description，
        返回元数据供 /skill search 命令或 find-skills 技能使用。

        Args:
            query: 关键词（大小写不敏感，匹配 name 或 description）。

        Returns:
            list[dict]：每项含 name / description / category / path / is_active。

        Raises:
            不主动抛异常；单个 SKILL.md 解析失败则跳过。

        组装规则：
            1. 若 builtin 根目录不存在 → 返回空列表。
            2. rglob 遍历所有 SKILL.md，逐个 parse_skill_file（origin=BUILTIN）。
            3. 解析失败 → 跳过；name / description 命中 query → 收集。
            4. category 取 skill_md.parent.parent.name（如 core / research）。
            5. is_active 由当前 active 技能名集合判断。
        """
        from poirot.backend.agents.skill.parser import parse_skill_file

        if not _BUILTIN_SKILLS_DIR.exists():
            return []
        query_lower = query.lower()
        results: list[dict] = []
        for skill_md in sorted(_BUILTIN_SKILLS_DIR.rglob("SKILL.md")):
            try:
                rec = parse_skill_file(skill_md, origin="BUILTIN")
            except Exception:
                continue
            # 匹配 name 或 description
            if query_lower in rec.name.lower() or query_lower in rec.description.lower():
                category = skill_md.parent.parent.name  # core / research / ...
                results.append({
                    "name": rec.name,
                    "description": rec.description,
                    "category": category,
                    "path": str(skill_md),
                    "is_active": rec.name in {r["name"] for r in self.list_skills()},
                })
        return results


def build_skill_manager() -> SkillManager | None:
    """SkillManager 工厂。

    从 .env 读取 POIROT_SKILL_ENABLED 与技能目录：
    - 未启用，或没有任何技能目录存在，返回 None；
    - 否则返回构造好的 SkillManager（尚未 load_startup）。

    Returns:
        SkillManager 实例；未启用或无目录时返回 None。

    组装规则：
        1. load_skill_config() 读配置。
        2. config.enabled 为假 → 返回 None。
        3. 所有 skill_dirs 均不存在 → 返回 None。
        4. 否则返回 SkillManager(config)（轻装配，仅建 store）。
    """
    config = load_skill_config()
    if not config.enabled:
        return None
    if not any(Path(d).exists() for d in config.skill_dirs):
        return None
    return SkillManager(config)