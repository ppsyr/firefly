"""线程状态与状态类型 — LangGraph 图的状态 schema 及承载类型。

【整体职责】
定义 Poirot 研究线程的 LangGraph state schema（ThreadState）及状态中承载的全部
数据类型：意图、计划、观察、来源、引用、产物、反思项、错误、编排层状态等。
字段级 Annotated reducer 驱动图内部的状态合并。

【内容摘要】
- GovernanceState        : 治理层共享状态类型别名（策略 bundle 自管命名空间）。
- OrchestrationState     : Multi-Agent 编排层状态（specialist 产物 + 活跃 specialist）。
- IntentState            : 意图状态（任务类型、深度、目标、约束等）。
- PlanStep               : 计划步骤。
- ResearchPlan           : 研究计划。
- Observation            : 观察（证据条目）。
- Source                 : 来源。
- Citation               : 引用。
- Artifact               : 产物。
- ReflectionItem         : 反思项。
- AgentError             : 错误（兼工具调用账本）。
- ThreadState            : 线程状态 schema，继承 AgentState，含字段级 reducer。

【职责边界】
- 只负责：定义状态 schema、字段语义、承载的数据类型。
- 不负责：字段合并的具体实现（reducers 模块）、状态的读写与持久化
  （runtime / checkpointer）、使用状态的业务逻辑（middleware / agent）。

【INVARIANT】
- 继承 AgentState：ThreadState 继承自 AgentState，间接获得 messages（带 add_messages reducer）。
- 字段级 reducer：列表/字典类字段用 Annotated[..., merge_xxx] 驱动图内合并。
- NotRequired 表可选：未标注 reducer 的字段用 NotRequired，表示图不强制提供。
- frozen 数据类型：承载类型均为 frozen dataclass，元组字段用 default_factory=tuple 避免可变默认值。
- 编排产物分离：OrchestrationState.specialist_artifacts 独立于 ThreadState.artifacts，不混用。
- 治理层命名空间：governance 由策略 bundle 自管，不预设固定槽，全值可 JSON 序列化。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, NotRequired, TypedDict

from langchain.agents import AgentState

from poirot.backend.agents.state.reducers import (
    merge_artifacts,
    merge_citations,
    merge_errors,
    merge_final_report,
    merge_governance,
    merge_memory_recalled,
    merge_memory_updates,
    merge_metadata,
    merge_observations,
    merge_orchestration,
    merge_reflection_items,
    merge_sandbox,
    merge_sources,
    merge_tagged_context,
    merge_todos,
)

# 治理层共享状态：策略 bundle 自管命名空间 governance.<strategy_name>.*。
# 族无关，不预设固定槽（原 6 固定槽是 volume 族 schema，已删）。
# merge_governance deep-merge（last-write-wins per leaf key）。
# 全值可 JSON 序列化（str/int/dict/list），禁 file handle 等不可序列化对象。
GovernanceState = dict[str, Any]


class OrchestrationState(TypedDict, total=False):
    """Multi-Agent 编排层状态（specialist 产物 + 活跃 specialist）。

    与 lead agent artifacts 分离——specialist 产物写 specialist_artifacts，
    不混入 ThreadState.artifacts（design.md §2 artifacts 分离）。
    merge_orchestration 去重追加（specialist_artifacts 按 path / active_specialists 按 name）。

    Attributes:
        specialist_artifacts: specialist 产物引用列表（list[ArtifactRef]）。
        active_specialists: 当前活跃 specialist 名列表。
    """

    specialist_artifacts: list  # list[ArtifactRef]
    active_specialists: list[str]


@dataclass(frozen=True)
class IntentState:
    """意图状态。

    Attributes:
        task_type: 任务类型。
        depth: 研究深度。
        objective: 目标。
        constraints: 约束集合。
        output_format: 输出格式。
        clarification_needed: 是否需要澄清。
    """

    task_type: str
    depth: str
    objective: str
    constraints: tuple[str, ...] = field(default_factory=tuple)
    output_format: str = "markdown_report"
    clarification_needed: bool = False


@dataclass(frozen=True)
class PlanStep:
    """计划步骤。

    Attributes:
        step_id: 步骤 ID。
        title: 标题。
        description: 描述。
        status: 状态，默认 pending。
    """

    step_id: str
    title: str
    description: str = ""
    status: str = "pending"


@dataclass(frozen=True)
class ResearchPlan:
    """研究计划。

    Attributes:
        plan_id: 计划 ID。
        goal: 计划目标。
        steps: 步骤集合。
        status: 状态，默认 pending。
    """

    plan_id: str
    goal: str
    steps: tuple[PlanStep, ...] = field(default_factory=tuple)
    status: str = "pending"


@dataclass(frozen=True)
class Observation:
    """观察（证据条目）。

    Attributes:
        observation_id: 观察 ID。
        step_id: 关联步骤 ID，可选。
        content: 内容。
        source_refs: 来源引用集合。
        created_at: 创建时间，可选。
    """

    observation_id: str
    step_id: str | None
    content: str
    source_refs: tuple[str, ...] = field(default_factory=tuple)
    created_at: str | None = None


@dataclass(frozen=True)
class Source:
    """来源。

    Attributes:
        source_id: 来源 ID。
        url: URL。
        title: 标题。
        source_type: 来源类型，默认 web。
        retrieved_at: 获取时间，可选。
        summary: 摘要。
    """

    source_id: str
    url: str
    title: str = ""
    source_type: str = "web"
    retrieved_at: str | None = None
    summary: str = ""


@dataclass(frozen=True)
class Citation:
    """引用。

    Attributes:
        citation_id: 引用 ID。
        source_id: 关联来源 ID。
        quote: 引文。
        claim: 论断。
        location: 位置，可选。
    """

    citation_id: str
    source_id: str
    quote: str
    claim: str
    location: str | None = None


@dataclass(frozen=True)
class Artifact:
    """产物。

    Attributes:
        artifact_id: 产物 ID。
        artifact_type: 产物类型。
        title: 标题。
        path: 路径。
        summary: 摘要。
        source_refs: 来源引用集合。
        created_at: 创建时间，可选。
    """

    artifact_id: str
    artifact_type: str
    title: str
    path: str
    summary: str = ""
    source_refs: tuple[str, ...] = field(default_factory=tuple)
    created_at: str | None = None


@dataclass(frozen=True)
class ReflectionItem:
    """反思项。

    Attributes:
        item_id: 项 ID。
        scope: 范围。
        kind: 种类。
        question: 问题。
        status: 状态，默认 open。
        related_refs: 关联引用集合。
        created_at: 创建时间，可选。
    """

    item_id: str
    scope: str
    kind: str
    question: str
    status: str = "open"
    related_refs: tuple[str, ...] = field(default_factory=tuple)
    created_at: str | None = None


@dataclass(frozen=True)
class AgentError:
    """错误（兼工具调用账本）。

    Attributes:
        error_id: 错误 ID。
        stage: 发生阶段。
        message: 错误信息。
        related_refs: 关联引用集合。
        created_at: 创建时间，可选。
        kind: 类型，"failure" / "success"。
        tool_name: 工具名。
        attempt: 该 tool 连续失败次数（成功归 0）。
        error_type: F5 分类。
        reason: F8.2 原因模板。
    """

    error_id: str
    stage: str
    message: str
    related_refs: tuple[str, ...] = field(default_factory=tuple)
    created_at: str | None = None
    # F8.1：errors 升级为工具调用账本，扩展字段（带默认值兼容既有构造）
    kind: str = "failure"           # "failure" / "success"
    tool_name: str = ""
    attempt: int = 0                # 该 tool 连续失败次（成功归 0）
    error_type: str = ""            # F5 分类
    reason: str = ""                # F8.2 原因模板


class ThreadState(AgentState):
    """LangGraph state schema for Poirot research threads.

    Extends AgentState (inherits messages with add_messages reducer).
    Field-level Annotated reducers drive graph-internal state merge.

    Attributes:
        user_input: 用户输入。
        intent: 意图。
        research_question: 研究问题。
        plan: 研究计划。
        current_step_id: 当前步骤 ID。
        observations: 观察列表（merge_observations）。
        sources: 来源列表（merge_sources）。
        citations: 引用列表（merge_citations）。
        artifacts: 产物列表（merge_artifacts）。
        reflection_items: 反思项列表（merge_reflection_items）。
        final_report: 最终报告（merge_final_report）。
        errors: 错误列表（merge_errors）。
        metadata: 元数据（merge_metadata）。
        todos: 待办列表（merge_todos）。
        governance: 治理层状态（merge_governance）。
        tagged_context: 标签化上下文（merge_tagged_context）。
        sandbox: 沙箱状态（merge_sandbox）。
        orchestration: 编排层状态（merge_orchestration）。
        help_request_count: 求助次数。
        pending_help: 待处理求助。
        skill_suggestion: 技能建议。
        recalled_memories: 召回的记忆（merge_memory_recalled）。
        memory_updates: 记忆更新（merge_memory_updates）。
    """

    user_input: NotRequired[str]
    intent: NotRequired[Any]
    research_question: NotRequired[str]
    plan: NotRequired[Any]
    current_step_id: NotRequired[str | None]
    observations: Annotated[list, merge_observations]
    sources: Annotated[list, merge_sources]
    citations: Annotated[list, merge_citations]
    artifacts: Annotated[list, merge_artifacts]
    reflection_items: Annotated[list, merge_reflection_items]
    final_report: Annotated[str | None, merge_final_report]
    errors: Annotated[list, merge_errors]
    metadata: Annotated[dict, merge_metadata]
    todos: Annotated[list | None, merge_todos]
    governance: Annotated[GovernanceState | None, merge_governance]
    tagged_context: Annotated[dict | None, merge_tagged_context]
    sandbox: Annotated[dict | None, merge_sandbox]
    orchestration: Annotated[OrchestrationState | None, merge_orchestration]
    help_request_count: NotRequired[int]
    pending_help: NotRequired[dict | None]
    skill_suggestion: NotRequired[list[dict] | None]
    recalled_memories: Annotated[list | None, merge_memory_recalled]
    memory_updates: Annotated[list | None, merge_memory_updates]