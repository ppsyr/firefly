"""Multi-Agent 核心数据契约 — frozen dataclass。

【整体职责】
定义 multiagent 层在 Leader 与 specialist / subagent 之间传递的全部数据结构：
请求（Request）、原始结果（RawResult）、最终结果（Result）、能力声明、产物引用、token 用量。
供 registry / specialist / runtime / summarizer / middleware 共同引用，保证跨 specialist 语义一致。

【内容摘要】
- SpecialistCapability    : 能力枚举（coding / research / review / planning）。
- SpecialistCapabilities  : specialist 的能力声明，支持 has() 查询。
- TokenUsage              : token 用量（specialist 自报，可选）。
- ArtifactRef             : specialist 产物的轻量引用（不含全局 artifact_id）。
- SpecialistRequest       : 派活给 specialist 的请求结构。
- SpecialistRawResult     : specialist runtime 返回的原始输出，交给 ResultSummarizer 消费。
- SpecialistResult        : ResultSummarizer 压缩后回传 Leader 的结果结构。
- SubagentRequest         : 派活给自复制 subagent 的请求结构（与 SpecialistRequest 同构）。
- SubagentResult          : 自复制 subagent 返回的结果结构（与 SpecialistResult 同构）。

【职责边界】
- 只负责：定义数据结构、字段语义、枚举。
- 不负责：上下文摘要逻辑（ContextSummarizer）、结果摘要逻辑（ResultSummarizer）、
  specialist 执行（runtime）、能力注册与过滤（registry）。
- 不持有状态：全部为 frozen dataclass，构造后不可变。

【INVARIANT】
- 全部 dataclass frozen：保证线程安全，可跨 Agent / 跨线程共享。
- specialist 黑盒：Poirot 只传 goal + success_criteria + context_summary（+ 可选 sandbox_id），
  不暴露 specialist 内部状态。
- success_criteria 必填：由 tool handler 强制，ResultSummarizer 据此校验。
- success=False 时 gap_analysis 必填：programmatic eval floor。
- artifacts 分离：specialist 产物走 ArtifactRef，写入 orchestration.specialist_artifacts，
  不与 lead agent 的 state.Artifact 混用。
- TokenUsage 可选：specialist 可能不暴露 token 用量，None 表示未上报。
- SubagentRequest / SubagentResult 与 Specialist 同构：便于 OrchestrationMiddleware 统一处理。
- 向后兼容：SpecialistResult.failure_category 默认 None，L2 FailureFocuser 按需读取。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class SpecialistCapability(str, Enum):
    """Specialist 能力枚举。list_specialists 按能力过滤时使用。"""

    CODING = "coding"
    RESEARCH = "research"
    REVIEW = "review"
    PLANNING = "planning"


@dataclass(frozen=True)
class SpecialistCapabilities:
    """Specialist 能力声明。register 时填写，list_specialists 据此过滤。"""

    capabilities: tuple[SpecialistCapability, ...] = ()

    def has(self, capability: SpecialistCapability) -> bool:
        """判断是否具备某能力。"""
        return capability in self.capabilities


@dataclass(frozen=True)
class TokenUsage:
    """specialist 调用的 token 用量。由 specialist 自报，可选。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class ArtifactRef:
    """specialist 产物引用。写入 orchestration.specialist_artifacts。

    与 state.Artifact 的区别：ArtifactRef 是 specialist 产物的轻量引用，
    不带全局 artifact_id（specialist 黑盒，Poirot 不为其分配 ID）。
    """

    path: str
    artifact_type: str
    specialist_name: str
    description: str = ""
    created_at: str | None = None


@dataclass(frozen=True)
class SpecialistRequest:
    """派活给 specialist 的请求结构。

    由 tool handler 内部构造：LLM 只填 goal + success_criteria（+ 可选 sandbox_id），
    其余字段从 config 与 ContextSummarizer 自动生成。
    """

    goal: str
    success_criteria: str
    context_summary: str
    sandbox_id: str | None
    artifacts_path: str | None
    max_steps: int = 50
    timeout_seconds: int = 600
    allowed_tools: tuple[str, ...] = ()
    skill_injection: str | None = None


@dataclass(frozen=True)
class SpecialistRawResult:
    """specialist runtime 返回的原始输出。

    由 ResultSummarizer 消费，压缩为 SpecialistResult 后回传 Leader。
    """

    raw_output: str
    artifacts: tuple[ArtifactRef, ...] = ()
    usage: TokenUsage | None = None
    duration_seconds: float = 0.0
    exit_code: int = 0


@dataclass(frozen=True)
class SpecialistResult:
    """specialist 调用结果。由 ResultSummarizer 生成，回传 Leader。

    - success=False 时 gap_analysis 必填（programmatic eval floor）。
    - error 仅失败时填（错误类型 + 简述，不暴露 specialist 内部状态）。
    - failure_category 供 L2 FailureFocuser 读取，默认 None 保证向后兼容。
    """

    specialist_name: str
    summary: str
    artifacts: tuple[ArtifactRef, ...] = ()
    success: bool = False
    gap_analysis: str = ""
    usage: TokenUsage | None = None
    duration_seconds: float = 0.0
    error: str | None = None
    failure_category: str | None = None


@dataclass(frozen=True)
class SubagentRequest:
    """派活给自复制 subagent 的请求结构（leaf role，复用 lead factory）。

    与 SpecialistRequest 同构，便于 OrchestrationMiddleware 统一处理。
    """

    goal: str
    success_criteria: str
    context_summary: str
    sandbox_id: str | None
    artifacts_path: str | None
    max_steps: int = 20
    timeout_seconds: int = 300
    allowed_tools: tuple[str, ...] = ()
    skill_injection: str | None = None


@dataclass(frozen=True)
class SubagentResult:
    """自复制 subagent 返回的结果结构（与 SpecialistResult 同构）。"""

    summary: str
    artifacts: tuple[ArtifactRef, ...] = ()
    success: bool = False
    gap_analysis: str = ""
    usage: TokenUsage | None = None
    duration_seconds: float = 0.0
    error: str | None = None