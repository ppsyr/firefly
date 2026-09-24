"""SpecialistRuntime Protocol — specialist 裸执行契约（sync only）。

【整体职责】
定义 specialist 的底层执行契约：只负责把 SpecialistRequest 跑成一个
SpecialistRawResult，不掺任何上下文摘要、结果压缩、状态管理。
上层由 SpecialistAgent 持有并组合使用。

【内容摘要】
- SpecialistRuntime(Protocol) : 裸执行契约，仅一个 invoke() 方法。

【职责边界】
- 只负责：执行 specialist loop，返回 raw output。
- 不负责：上下文摘要（ContextSummarizer）、结果摘要（ResultSummarizer）、
  线程状态管理（ThreadState）、凭证获取（CredentialProvider）。
- 不持有状态：Protocol 无实现，纯接口。

【INVARIANT】
- sync only：只定义 invoke，不提供 ainvoke（异步留待后续）。
- 低层纯执行：不感知 ContextSummarizer / ResultSummarizer / ThreadState。
- 异常统一：失败抛 SpecialistError 子类，不抛裸异常。
- 实现示例：CodexRuntime / ClaudeCodeRuntime / SubagentRuntime。
- 与 SpecialistAgent 是"被持有"关系：Agent 是高层封装，Runtime 是低层执行。
"""
from __future__ import annotations

from typing import Protocol

from poirot.backend.agents.multiagent.types import (
    SpecialistRawResult,
    SpecialistRequest,
)


class SpecialistRuntime(Protocol):
    """specialist runtime 裸执行契约（sync only）。

    实现示例：CodexRuntime / ClaudeCodeRuntime / SubagentRuntime。
    """

    def invoke(self, request: SpecialistRequest) -> SpecialistRawResult:
        """执行 specialist loop，返回 raw output。

        失败时抛 SpecialistError 子类（SpecialistTimeoutError /
        SpecialistCrashError / SpecialistStartupError / SpecialistCredentialError）。
        """
        ...