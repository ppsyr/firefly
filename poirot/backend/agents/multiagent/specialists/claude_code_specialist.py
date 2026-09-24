"""ClaudeCodeSpecialist — 组合 ClaudeCodeRuntime 的 Claude Code specialist。

【整体职责】
把 ClaudeCodeRuntime 适配为 SpecialistAgent 契约：对外表现为一个名为 "claude"、
能力为 REVIEW 的 specialist，内部只做转发——invoke 直接调 runtime.invoke。

【内容摘要】
- ClaudeCodeSpecialist : specialist 实现，组合 ClaudeCodeRuntime。

【职责边界】
- 只负责：暴露 name / capabilities / invoke 三成员，invoke 转发给 runtime。
- 不负责：实际执行（ClaudeCodeRuntime 负责）、上下文摘要、结果摘要、凭证获取。
- 不持有状态：内部只持有 runtime 引用。

【INVARIANT】
- specialist 黑盒：invoke 只调 runtime，不掺任何额外逻辑。
- name 固定为 "claude"。
- capabilities 固定为 (REVIEW,)。
- specialist 自带 model：claude CLI 自管 ReAct loop，Poirot 不介入其内部。
- runtime 可选：未传入时构造默认 ClaudeCodeRuntime()。
"""
from __future__ import annotations

from poirot.backend.agents.multiagent.runtimes.claude_code_runtime import (
    ClaudeCodeRuntime,
)
from poirot.backend.agents.multiagent.types import (
    SpecialistCapabilities,
    SpecialistCapability,
    SpecialistRawResult,
    SpecialistRequest,
)


class ClaudeCodeSpecialist:
    """Claude Code specialist。

    组合 ClaudeCodeRuntime，name 固定 "claude"，capability 固定 REVIEW。
    """

    def __init__(self, runtime: ClaudeCodeRuntime | None = None) -> None:
        """初始化。runtime 为 None 时构造默认 ClaudeCodeRuntime()。"""
        self._runtime = runtime or ClaudeCodeRuntime()

    @property
    def name(self) -> str:
        """specialist 名，固定 "claude"。"""
        return "claude"

    @property
    def capabilities(self) -> SpecialistCapabilities:
        """能力声明，固定 (REVIEW,)。"""
        return SpecialistCapabilities(
            capabilities=(SpecialistCapability.REVIEW,),
        )

    def invoke(self, request: SpecialistRequest) -> SpecialistRawResult:
        """执行 specialist 调用，直接转发给 runtime.invoke。"""
        return self._runtime.invoke(request)