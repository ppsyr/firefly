"""CodexSpecialist — 组合 CodexRuntime 的 Codex specialist。

【整体职责】
把 CodexRuntime 适配为 SpecialistAgent 契约：对外表现为一个名为 "codex"、
能力为 CODING 的 specialist，内部只做转发——invoke 直接调 runtime.invoke。

【内容摘要】
- CodexSpecialist : specialist 实现，组合 CodexRuntime。

【职责边界】
- 只负责：暴露 name / capabilities / invoke 三成员，invoke 转发给 runtime。
- 不负责：实际执行（CodexRuntime 负责）、上下文摘要、结果摘要、凭证获取。
- 不持有状态：内部只持有 runtime 引用。

【INVARIANT】
- specialist 黑盒：invoke 只调 runtime，不掺任何额外逻辑。
- name 固定为 "codex"。
- capabilities 固定为 (CODING,)。
- specialist 自带 model：runtime 启动 codex-acp 自管 ReAct loop，Poirot 不介入其内部。
- 凭证由 bootstrap 检测：缺失时 specialist 不注册，凭证不进 specialist。
- runtime 可选：未传入时构造默认 CodexRuntime()。
"""
from __future__ import annotations

from poirot.backend.agents.multiagent.runtimes.codex_runtime import CodexRuntime
from poirot.backend.agents.multiagent.types import (
    SpecialistCapabilities,
    SpecialistCapability,
    SpecialistRawResult,
    SpecialistRequest,
)


class CodexSpecialist:
    """Codex specialist。

    组合 CodexRuntime，name 固定 "codex"，capability 固定 CODING。
    """

    def __init__(self, runtime: CodexRuntime | None = None) -> None:
        """初始化。runtime 为 None 时构造默认 CodexRuntime()。"""
        self._runtime = runtime or CodexRuntime()

    @property
    def name(self) -> str:
        """specialist 名，固定 "codex"。"""
        return "codex"

    @property
    def capabilities(self) -> SpecialistCapabilities:
        """能力声明，固定 (CODING,)。"""
        return SpecialistCapabilities(
            capabilities=(SpecialistCapability.CODING,),
        )

    def invoke(self, request: SpecialistRequest) -> SpecialistRawResult:
        """执行 specialist 调用，直接转发给 runtime.invoke。"""
        return self._runtime.invoke(request)