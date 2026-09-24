"""SubagentProvider Protocol — Poirot self-copy subagent 契约。

【整体职责】
定义"自复制 subagent"的生成契约：让 Leader 能 spawn 一个与自己同构的子 Agent 执行任务。
子 Agent 复用 lead agent factory，但以 leaf role 运行、隔离上下文、共享沙箱。

【内容摘要】
- SubagentProvider(Protocol) : 自复制 subagent 契约，仅一个 spawn() 方法。

【职责边界】
- 只负责：定义 spawn 契约（方法签名 + 语义）。
- 不负责：具体 spawn 实现（SubagentRuntime 负责）、上下文摘要、结果摘要、
  与 specialist 体系的适配（SubagentSpecialist 负责）。
- 不持有状态：Protocol 无实现，纯接口。

【INVARIANT】
- Poirot self-copy：复用 lead agent factory（create_poirot_agent）创建子 Agent。
- leaf role 递归控制：子 Agent 的 tool_groups 不含 multiagent，无法再 spawn（防无限递归）。
- isolated context：子 Agent 用全新 ThreadState，不继承父 message history；
  只通过 goal + context_summary 传信息。
- shared thread sandbox：子 Agent 复用父 sandbox_id，不另起沙箱。
- sync only：spawn 同步返回，不提供异步版本。
- 异常统一：失败抛 SubagentError 子类（SubagentTimeoutError / SubagentMaxStepsError）。

【与 SpecialistRuntime 的区别】
- SubagentProvider.spawn(SubagentRequest) -> SubagentResult：高层 API，自复制专用。
- SpecialistRuntime.invoke(SpecialistRequest) -> SpecialistRawResult：低层 API，通用执行契约。
- SubagentSpecialist 组合 SubagentRuntime，把 SubagentProvider 适配为 SpecialistRuntime。
"""
from __future__ import annotations

from typing import Protocol

from poirot.backend.agents.multiagent.types import (
    SubagentRequest,
    SubagentResult,
)


class SubagentProvider(Protocol):
    """Poirot self-copy subagent 契约（sync only，leaf role）。

    实现示例：SubagentRuntime 内部构造 lead factory 调用。
    """

    def spawn(self, request: SubagentRequest) -> SubagentResult:
        """spawn 一个 Poirot self-copy subagent 执行任务。

        - leaf role：子 Agent 的 tool_groups 不含 multiagent，看不到 delegate_to_* 工具。
        - isolated context：全新 ThreadState，只传 goal + context_summary。
        - shared thread sandbox：复用父 sandbox_id。

        失败时抛 SubagentError 子类（SubagentTimeoutError / SubagentMaxStepsError）。
        """
        ...