"""SubagentSpecialist — 组合 SubagentRuntime 的 self-copy specialist。

【整体职责】
把 SubagentRuntime 适配为 SpecialistAgent 契约：对外表现为一个名为 "subagent"、
能力为 RESEARCH 的 specialist，内部只做转发——invoke 直接调 runtime.invoke。

【内容摘要】
- SubagentSpecialist : specialist 实现，组合 SubagentRuntime。

【职责边界】
- 只负责：暴露 name / capabilities / invoke 三成员，invoke 转发给 runtime。
- 不负责：实际执行（SubagentRuntime 负责）、上下文摘要、结果摘要、凭证获取。
- 不持有状态：内部只持有 runtime 引用。

【INVARIANT】
- specialist 黑盒：invoke 只调 runtime，不掺任何额外逻辑。
- name 固定为 "subagent"。
- capabilities 固定为 (RESEARCH,)。
- leaf role 递归控制：由 runtime 保证子 agent 的 tool_groups 不含 multiagent。
- runtime 可选：未传入时构造默认 SubagentRuntime()。
"""
from __future__ import annotations

from poirot.backend.agents.multiagent.runtimes.subagent_runtime import (
    SubagentRuntime,
)
from poirot.backend.agents.multiagent.types import (
    SpecialistCapabilities,
    SpecialistCapability,
    SpecialistRawResult,
    SpecialistRequest,
)


class SubagentSpecialist:
    """Poirot self-copy subagent specialist。

    组合 SubagentRuntime，name 固定 "subagent"，capability 固定 RESEARCH。
    """

    def __init__(self, runtime: SubagentRuntime | None = None) -> None:
        """初始化。runtime 为 None 时构造默认 SubagentRuntime()。"""
        self._runtime = runtime or SubagentRuntime()

    @property
    def name(self) -> str:
        """specialist 名，固定 "subagent"。"""
        return "subagent"

    @property
    def capabilities(self) -> SpecialistCapabilities:
        """能力声明，固定 (RESEARCH,)。"""
        return SpecialistCapabilities(
            capabilities=(SpecialistCapability.RESEARCH,),
        )

    def invoke(self, request: SpecialistRequest) -> SpecialistRawResult:
        """执行 specialist 调用，直接转发给 runtime.invoke。"""
        return self._runtime.invoke(request)