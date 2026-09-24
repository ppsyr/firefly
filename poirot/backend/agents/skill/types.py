"""Skill 数据模型 — frozen dataclass + 4 rate property。

【整体职责】
定义 skill 子系统的核心数据结构（全部为不可变值对象）。
作为 parser / store / selector / middleware 之间的数据契约：
- parser 产出 SkillRecord；
- store 持久化并回读 SkillRecord / 计算 SkillMetrics / SkillHealth；
- selector / middleware 消费 SkillRecord 与其 rate。

【内容摘要】
- SkillLineage ：版本血缘（父节点 / 代数 / 来源 / 版本哈希 / 创建者）。
- SkillRecord  ：技能注册条目（元数据 + 4 计数器 + 4 rate property）。
- SkillMetrics ：质量指标快照（4 计数器 + 4 rate，由 store.get_metrics 返回）。
- SkillHealth  ：健康状态（由 store.health_check 返回，带 degraded 标记）。

【职责边界】
- 只负责：数据结构定义与纯计算 property（rate）。
- 不负责：持久化（store）、解析（parser）、选择（selector）、打点触发（middleware）。
- 不存全文：SKILL.md 内容留在文件（path），此处只存引用 + metrics。

【INVARIANT】
- 全部 frozen（不可变值对象），构造后字段不可改。
- SkillRecord 含 4 计数器（selections / applied / completions / fallbacks）
  与 4 rate property；rate 零除保护（分母为 0 时返 0.0）。
- SkillLineage.parent_skill_ids 语义：
    ()       → IMPORTED / CAPTURED（root）
    (prev,)  → FIXED（单父）
    (multi,) → DERIVED（多父）
- origin: IMPORTED / CAPTURED / FIXED / DERIVED。
- 内容在文件（path）；SkillRecord 只存引用 + metrics，不存全文。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SkillLineage:
    """技能版本血缘（不可变）。

    parent_skill_ids: 父节点 id 元组，按来源区分：
        ()       → IMPORTED / CAPTURED（root，无父）
        (prev,)  → FIXED（单父，固定版本演进）
        (multi,) → DERIVED（多父，派生自多个技能）
    generation:   距 root 的深度（root 为 0）。
    origin:       来源，IMPORTED / CAPTURED / FIXED / DERIVED。
    version_hash: SKILL.md 内容的 sha256。
    created_by:   创建者，"human" | 模型名 | None。
    """

    parent_skill_ids: tuple[str, ...] = ()
    generation: int = 0
    origin: str = "IMPORTED"
    version_hash: str = ""
    created_by: str | None = None


@dataclass(frozen=True)
class SkillRecord:
    """技能注册条目（不可变）。内容留在文件（path），此处只存引用 + metrics。

    身份与来源：
    - skill_id / name / path / content_hash：标识与文件引用。
    - is_active  : 单指针，每个 name 仅 1 个 active 版本。
    - lineage    : 版本血缘（SkillLineage）。
    - description / allowed_tools / enabled：元数据。

    质量打点：
    - 4 计数器：total_selections / total_applied / total_completions / total_fallbacks
      （基础层打点，由 store 的 record_selection / record_outcome 累加）。
    - 4 rate property：applied_rate / completion_rate / effective_rate / fallback_rate
      （只读，零除保护，分母为 0 时返 0.0）。

    时间戳：
    - created_at / last_updated：ISO 字符串。
    """

    skill_id: str
    name: str
    path: str
    content_hash: str
    is_active: bool = True
    lineage: SkillLineage = field(default_factory=SkillLineage)
    description: str = ""
    allowed_tools: tuple[str, ...] = ()
    enabled: bool = True
    total_selections: int = 0
    total_applied: int = 0
    total_completions: int = 0
    total_fallbacks: int = 0
    created_at: str = ""
    last_updated: str = ""

    @property
    def applied_rate(self) -> float:
        """应用率 = applied / selections；selections=0 时返 0.0。"""
        return self.total_applied / self.total_selections if self.total_selections else 0.0

    @property
    def completion_rate(self) -> float:
        """完成率 = completions / applied；applied=0 时返 0.0。"""
        return self.total_completions / self.total_applied if self.total_applied else 0.0

    @property
    def effective_rate(self) -> float:
        """有效率 = completions / selections；selections=0 时返 0.0。

        这是 selector 排序与 health_check 判定的主指标。
        """
        return self.total_completions / self.total_selections if self.total_selections else 0.0

    @property
    def fallback_rate(self) -> float:
        """回退率 = fallbacks / selections；selections=0 时返 0.0。"""
        return self.total_fallbacks / self.total_selections if self.total_selections else 0.0


@dataclass(frozen=True)
class SkillMetrics:
    """技能质量指标快照（不可变）。

    由 SQLiteSkillStore.get_metrics 返回，字段与 SkillRecord 的计数器 / rate 一一对应，
    用于把「计算好 rate 的结果」从 store 传出去，避免调用方重复算。
    """

    skill_id: str
    selections: int
    applied: int
    completions: int
    fallbacks: int
    applied_rate: float
    completion_rate: float
    effective_rate: float
    fallback_rate: float


@dataclass(frozen=True)
class SkillHealth:
    """技能健康状态（不可变）。由 SQLiteSkillStore.health_check 返回。

    degraded 判定：effective_rate < threshold 且 total_selections >= min_selections。
    （selections 不足时不判 degraded，给新技能积累数据的机会。）
    """

    skill_id: str
    name: str
    effective_rate: float
    fallback_rate: float
    total_selections: int
    degraded: bool