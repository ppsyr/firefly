"""SpecialistAgent Protocol — 专业 agent 高层抽象契约。

【整体职责】
定义 specialist 的高层契约：一个 specialist 必须能报名字、报能力、被调用执行。
Poirot 通过本契约把 specialist 当黑盒使用，不关心其内部 model、ReAct loop、context 管理。

【内容摘要】
- SpecialistAgent(Protocol)  : specialist 高层契约，含 name / capabilities / invoke 三成员。

【职责边界】
- 只负责：定义契约（属性 + 方法签名 + 语义）。
- 不负责：具体执行（runtime 负责）、上下文摘要（ContextSummarizer 负责）、
  结果摘要（ResultSummarizer 负责）、凭证获取（CredentialProvider 负责）。
- 不持有状态：Protocol 无实现，纯接口。

【INVARIANT】
- specialist 黑盒：自带 model、自管 ReAct loop、自管 context；Poirot 只传
  goal + context_summary + sandbox_id，不窥探内部。
- invoke 返回 SpecialistRawResult（raw output + artifacts + usage + duration + exit_code）。
- invoke 失败抛 SpecialistError 子类（Timeout / Crash / Startup / Credential）。
- 不继承 SpecialistRuntime：SpecialistAgent 是高层抽象，SpecialistRuntime 是低层裸执行，
  两者是"持有"关系而非继承关系。
- 实现示例：CodexSpecialist / ClaudeCodeSpecialist / SubagentSpecialist，
  组合 SpecialistRuntime + ContextSummarizer + ResultSummarizer + CredentialProvider。
- name 用于生成 delegate_to_<name> 工具 + Registry 注册。
"""
from __future__ import annotations

from typing import Protocol

from poirot.backend.agents.multiagent.types import (
    SpecialistCapabilities,
    SpecialistRawResult,
    SpecialistRequest,
)


class SpecialistAgent(Protocol):
    """专业 agent 契约（黑盒，自带 model + ReAct loop）。

    实现示例：CodexSpecialist / ClaudeCodeSpecialist / SubagentSpecialist。
    组合 SpecialistRuntime + ContextSummarizer + ResultSummarizer + CredentialProvider。
    """

    @property
    def name(self) -> str:
        """specialist 唯一名。用于生成 delegate_to_<name> 工具 + Registry 注册。"""
        ...

    @property
    def capabilities(self) -> SpecialistCapabilities:
        """specialist 能力声明。list_specialists 按能力过滤时使用。"""
        ...

    def invoke(self, request: SpecialistRequest) -> SpecialistRawResult:
        """执行 specialist 调用，返回原始输出。

        失败时抛 SpecialistError 子类（Timeout / Crash / Startup / Credential）。
        """
        ...