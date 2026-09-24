"""L2 演化产物 + 类型层 — frozen dataclass + Protocol。

【整体职责】
定义 L2 进化层的核心类型：演化产物 Protocol（EvolutionArtifact）与两种具体产物
（ContextSummaryTemplate / SkillInjectionTemplate）、失败记录与统计、specialist 候选、
预算记账、触发源与晋升决策枚举、演化任务与结果。全部 frozen，作为跨组件传递的值对象。

【内容摘要】
- EvolutionArtifact(Protocol)    : 演化产物契约（version / template_id / artifact_hash）。
- ContextExtractor / ContextFilter / SkillSelector : 三种子组件契约。
- ContextSummaryTemplate(frozen) : W2 演化产物（context 生成模板）。
- SkillInjectionTemplate(frozen) : W4 演化产物（skill 注入模板）。
- FailureCategory(Enum)          : 失败分类 4 类。
- FailureRecord / FailureStats   : 单条失败记录 + 失败聚焦统计。
- SpecialistCandidate(frozen)    : specialist 候选 metadata。
- CostRecord / BudgetRemaining / BudgetCheckResult : 预算三维度记账类型。
- TriggerSource(Enum)            : L2 触发源 5 类。
- PromotionDecision(Enum)        : 晋升决策 3 类。
- EvolutionTask / EvolutionResult : 演化任务与结果。

【职责边界】
- 只负责：定义数据结构、字段语义、枚举、Protocol 契约。
- 不负责：演化逻辑（mutator 负责）、失败聚类（focuser 负责）、
  晋升判定（promotion_gate 负责）、预算记账（budget_guard 负责）。
- 不持有状态：全部 frozen dataclass 或 Protocol。

【INVARIANT】
- 全部 dataclass frozen：不可变，符合 Poirot 值对象原则。
- 演化产物形态 = 结构化 dataclass：可 version DAG / diff / 回滚。
- artifact_hash 由 payload 计算（@property）：防环用；同一 payload 得同一 hash。
- 演化产物不进 system prompt cache prefix：per-call 产物，hot swap 不破 cache。
- 可演化的失败类别：CONTEXT_INSUFFICIENT / ABILITY_INSUFFICIENT；
  GOAL_UNCLEAR / SANDBOX_ISSUE 不演化（转告警）。
- SpecialistCandidate 不含 capability_match：LLM 自决。
- BudgetCheckResult.allowed=False 时 reason 非空；fallback_target 固定 "lead"。
- PromotionDecision：ACCEPT / REJECT / FAILED 三态。
- EvolutionResult：ACCEPT 时 new_artifact_id 非空；REJECT/FAILED 时为 None。
- FailureStats.sample_failures：每类 top 2 样本，上限 5。
- CostRecord / BudgetRemaining 三维度：tokens / cost_usd / calls。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EvolutionArtifact(Protocol):
    """演化产物契约（runtime_checkable）。

    只声明属性接口，不实现字段。具体产物用 frozen dataclass 各自定义。
    artifact_hash 由 payload 计算得出（防环用）。
    """

    @property
    def version(self) -> str: ...

    @property
    def template_id(self) -> str: ...

    @property
    def artifact_hash(self) -> str: ...


class ContextExtractor(Protocol):
    """从 ThreadState 提取 context 片段（per-specialist 实现）。

    extractors 顺序执行，输出拼接后进 filters。
    """

    def extract(self, state: dict, goal: str) -> str: ...


class ContextFilter(Protocol):
    """过滤 / 截断 context（per-specialist 实现）。

    filters 顺序执行，前一个输出作后一个输入。
    """

    def filter(self, content: str, max_tokens: int) -> str: ...


class SkillSelector(Protocol):
    """根据 goal 选 skill（per-specialist 实现）。

    select 返回 tuple[Skill, ...]，按 max_skills 截断。
    """

    def select(self, goal: str, available_skills: list[Any]) -> tuple[Any, ...]: ...


@dataclass(frozen=True)
class ContextSummaryTemplate:
    """W2：ContextSummarizer 用的模板，控制 specialist 调用前 context 如何生成。

    演化产物：L2 EvolutionMutator 演化此模板（加 extractor / 调 max_tokens / 改 prompt 骨架）。
    不进 system prompt cache prefix——per-call 产物，hot swap 不破 cache。
    artifact_hash 由 payload（extractors + filters + max_tokens + prompt_skeleton）计算。
    """

    version: str
    template_id: str
    extractors: tuple[ContextExtractor, ...]
    filters: tuple[ContextFilter, ...]
    max_tokens: int
    prompt_skeleton: str

    @property
    def artifact_hash(self) -> str:
        """由 payload 计算 hash（取 sha256 前 16 位）。"""
        payload = json.dumps({
            "version": self.version,
            "template_id": self.template_id,
            "extractors": [type(e).__name__ for e in self.extractors],
            "filters": [type(f).__name__ for f in self.filters],
            "max_tokens": self.max_tokens,
            "prompt_skeleton": self.prompt_skeleton,
        }, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class SkillInjectionTemplate:
    """W4：SpecialistRequest.skill_injection 生成模板。

    演化产物：L2 EvolutionMutator 演化此模板（换 selector / 加 max_skills / 改注入格式）。
    不进 system prompt cache prefix——per-call 产物，hot swap 不破 cache。
    artifact_hash 由 payload（skill_selector + injection_format + max_skills）计算。
    """

    version: str
    template_id: str
    skill_selector: SkillSelector
    injection_format: str
    max_skills: int = 3

    @property
    def artifact_hash(self) -> str:
        """由 payload 计算 hash（取 sha256 前 16 位）。"""
        payload = json.dumps({
            "version": self.version,
            "template_id": self.template_id,
            "skill_selector": type(self.skill_selector).__name__,
            "injection_format": self.injection_format,
            "max_skills": self.max_skills,
        }, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class FailureCategory(Enum):
    """L1 ResultSummarizer 输出的失败分类（L2 FailureFocuser 读取）。

    CONTEXT_INSUFFICIENT / ABILITY_INSUFFICIENT 可演化（→ W2 / W4）。
    GOAL_UNCLEAR / SANDBOX_ISSUE 不演化（转告警，不进 L2 流程）。
    """

    GOAL_UNCLEAR = "goal_unclear"
    CONTEXT_INSUFFICIENT = "context_insufficient"
    ABILITY_INSUFFICIENT = "ability_insufficient"
    SANDBOX_ISSUE = "sandbox_issue"


@dataclass(frozen=True)
class FailureRecord:
    """单条失败记录（L2 FailureFocuser 聚类取 top 样本用）。

    severity 用于聚类排序——越大越优先取。
    """

    specialist_name: str
    goal: str
    success_criteria: str
    failure_category: FailureCategory
    raw_output_tail: str = ""
    severity: float = 0.0
    timestamp: str = ""


@dataclass(frozen=True)
class FailureStats:
    """失败聚焦统计（FailureFocuser.analyze 输出，喂给 EvolutionMutator）。

    - dominant_category：占比最高的可演化类别
      （GOAL_UNCLEAR / SANDBOX_ISSUE 不作主导）。
    - sample_failures：每类 top 2 样本（上限 5）。
    """

    by_category: dict[FailureCategory, int]
    dominant_category: FailureCategory | None
    sample_failures: dict[FailureCategory, list[FailureRecord]]


@dataclass(frozen=True)
class SpecialistCandidate:
    """specialist 候选 metadata（IntentEngineStrengthened 生成，供 ContextSummarizer 渲染）。

    不含 capability_match——LLM 自决。
    historical_success_rate：过去 N 次（N=20）success_criteria_met=true 的比例。
    sample_size < 20 时 LLM 可据此判断可信度。
    """

    name: str
    historical_success_rate: float
    avg_cost_usd: float
    avg_latency_seconds: float
    sample_size: int


@dataclass(frozen=True)
class CostRecord:
    """单次 specialist 调用成本（BudgetGuard 记账用，三维度）。

    cost_usd 由 token × model price 计算（MVP 用 config 默认 price）。
    """

    tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 1


@dataclass(frozen=True)
class BudgetRemaining:
    """budget 剩余量（BudgetCheckResult.remaining 用）。

    三维度：tokens / cost_usd / calls；per-day UTC 0 点重置。
    """

    tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0


@dataclass(frozen=True)
class BudgetCheckResult:
    """BudgetGuard.check_and_record 返回（超限 fallback lead）。

    - allowed=False 时 reason 非空
      （"daily_cost_exceeded" / "daily_tokens_exceeded" / "daily_calls_exceeded"）。
    - fallback_target 固定 "lead"（不 fallback 另一 specialist）。
    """

    allowed: bool
    specialist_name: str
    reason: str | None
    remaining: BudgetRemaining | None
    fallback_target: str = "lead"


class TriggerSource(Enum):
    """L2 触发源（TriggerManager 四源 + 节流）。

    - PERIODIC：6h cron 兜底。
    - FAILURE_FOCUSED：24h 窗口内某 failure_category ≥ 5 次。
    - SPECIALIST_DEGRADED：invoked ≥ 5 + completion_rate < 0.4。
    - COST_ALERT：单次 cost > $1。
    - LATENCY_ALERT：单次 latency > 5min。
    """

    PERIODIC = "periodic"
    FAILURE_FOCUSED = "failure_focused"
    SPECIALIST_DEGRADED = "specialist_degraded"
    COST_ALERT = "cost_alert"
    LATENCY_ALERT = "latency_alert"


class PromotionDecision(Enum):
    """PromotionGate 决策（hash 防环 + 95% CI）。

    - ACCEPT：candidate CI 下界 > baseline CI 上界。
    - REJECT：CI 重叠 / hash 命中近 5 版（防环）。
    - FAILED：演化或 eval 失败（保持旧 is_active）。
    """

    ACCEPT = "accept"
    REJECT = "reject"
    FAILED = "failed"


@dataclass(frozen=True)
class EvolutionTask:
    """L2 演化任务（TriggerMiddleware enqueue → cron queue → Worker 消费）。

    - profile：演化 profile（per-profile 串行锁 key）。
    - trigger_source + trigger_detail：触发源 + 详情（写 OrchestrationMetricsL2）。
    """

    task_id: str
    profile: str
    trigger_source: TriggerSource
    trigger_detail: str = ""
    artifact_type: str = "context_summary"
    timestamp: str = ""


@dataclass(frozen=True)
class EvolutionResult:
    """L2EvolutionWorker.run 返回（编排闭环结果）。

    - decision=ACCEPT 时 new_artifact_id 非空（VersionDAG commit 后的 id）。
    - decision=REJECT/FAILED 时 new_artifact_id=None（保持旧 is_active）。
    """

    task_id: str
    decision: PromotionDecision
    new_artifact_id: str | None = None
    rationale: str = ""
    error: str | None = None