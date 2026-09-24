"""PiSpecialist — 组合 PiRuntime 的 Pi coding specialist。

【整体职责】
把 PiRuntime 适配为 SpecialistAgent 契约：对外表现为一个名为 "pi"、能力为 CODING
的 specialist，内部只做转发——invoke 直接调 runtime.invoke。凭证由 bootstrap 检测后
传入，仅保存在实例上供 runtime 使用。

【内容摘要】
- PiSpecialist : specialist 实现，组合 PiRuntime。

【职责边界】
- 只负责：暴露 name / capabilities / invoke 三成员，invoke 转发给 runtime。
- 不负责：实际执行（PiRuntime 负责）、上下文摘要、结果摘要、凭证探测。
- 不持有运行时状态：内部只持有 runtime 引用 + credential 引用。

【INVARIANT】
- specialist 黑盒：invoke 只调 runtime，不掺任何额外逻辑。
- name 固定为 "pi"。
- capabilities 固定为 (CODING,)。
- specialist 自带 model：pi CLI 自管 ReAct loop，Poirot 不介入其内部。
- 凭证由 bootstrap 检测：缺失时 specialist 不注册；检测到后传入 specialist，
  仅保存在实例上供 runtime 使用，不写 ThreadState。
- 强制走 Poirot SpecialistMcpServer：由 PiRuntime 内部通过
  `--no-builtin-tools` + `-e poirot-sandbox-bridge` 实现，本类不参与。
- runtime 可选：未传入时构造默认 PiRuntime()。
"""
from __future__ import annotations

from typing import Any

from poirot.backend.agents.multiagent.runtimes.pi_runtime import (
    PiRuntime,
    PiRuntimeConfig,
)
from poirot.backend.agents.multiagent.types import (
    SpecialistCapabilities,
    SpecialistCapability,
    SpecialistRawResult,
    SpecialistRequest,
)


class PiSpecialist:
    """Pi coding specialist。

    组合 PiRuntime，name 固定 "pi"，capability 固定 CODING。
    与 CodexSpecialist / ClaudeCodeSpecialist 并列，作为第四个 specialist。
    """

    def __init__(
        self,
        runtime: PiRuntime | None = None,
        credential: Any | None = None,
    ) -> None:
        """初始化。

        Args:
            runtime: PiRuntime 实例；为 None 时构造默认 PiRuntime()。
            credential: 由 bootstrap 检测后传入的凭证；仅保存在实例上供 runtime 用。
        """
        self._runtime = runtime or PiRuntime()
        self._credential = credential  # 凭证由 bootstrap 检测，传给 specialist 仅供 runtime 用

    @property
    def name(self) -> str:
        """specialist 名，固定 "pi"。"""
        return "pi"

    @property
    def capabilities(self) -> SpecialistCapabilities:
        """能力声明，固定 (CODING,)。"""
        return SpecialistCapabilities(
            capabilities=(SpecialistCapability.CODING,),
        )

    def invoke(self, request: SpecialistRequest) -> SpecialistRawResult:
        """执行 specialist 调用，直接转发给 runtime.invoke。"""
        return self._runtime.invoke(request)