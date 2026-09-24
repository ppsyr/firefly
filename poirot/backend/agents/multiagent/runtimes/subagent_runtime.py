"""SubagentRuntime — 进程内调用 lead factory 实现（Poirot self-copy subagent）。

【整体职责】
实现 SpecialistRuntime 契约：在进程内调用 lead agent factory 创建一个 leaf-role 子 Agent，
用隔离的 ThreadState 执行任务，返回 SpecialistRawResult。
是 Poirot self-copy subagent 的底层执行器。

【内容摘要】
- SubagentRuntime              : runtime 主类，实现 invoke()。
- invoke()                     : 执行入口：取 agent → 建隔离 state → invoke → 抽输出。
- _get_agent()                 : 从 agent_factory 取子 Agent；未配置时抛 StartupError。
- _create_isolated_state()     : 构造全新 ThreadState（只带 goal + context_summary）。
- _extract_output()            : 从 agent 结果中抽取最后一条消息的文本。

【职责边界】
- 只负责：取 agent、建隔离 state、调 agent.invoke、抽取输出、异常归一化。
- 不负责：agent 的构造（agent_factory 提供）、上下文摘要（ContextSummarizer 负责）、
  结果摘要（ResultSummarizer 负责）、沙箱管理（sandbox 层负责）。
- 不持有运行时状态：每次 invoke 都新建 state，不跨调用累积。

【INVARIANT】
- sync only：只实现同步 invoke，不提供异步版本。
- 复用 lead agent factory：子 Agent 由 agent_factory 提供，与 Leader 同构。
- leaf role 递归控制：agent_factory 须返回不含 multiagent 工具的子 Agent，
  从工具层面杜绝无限递归。
- isolated context：全新 ThreadState，不继承父 messages / observations / sources；
  只通过 goal + context_summary 传信息。
- shared thread sandbox：request.sandbox_id 存在时写入 state["sandbox"]，复用父沙箱。
- max_steps 限制：传给 agent.invoke 的 recursion_limit = max_steps * 2；
  触发 RecursionError 时转 SubagentMaxStepsError。
- 异常归一化：agent_factory 未配置 → SpecialistStartupError；
  其他执行异常 → SpecialistCrashError。
- agent_factory 为 None 时，_get_agent 抛 SpecialistStartupError（bootstrap 必须注入）。
"""
from __future__ import annotations

import time
from typing import Any, Callable

from poirot.backend.agents.multiagent.exceptions import (
    SpecialistCrashError,
    SpecialistStartupError,
)
from poirot.backend.agents.multiagent.types import (
    SpecialistRawResult,
    SpecialistRequest,
)


class SubagentRuntime:
    """进程内调用 lead factory 的 self-copy subagent runtime。

    sync only；leaf role；isolated context；shared thread sandbox。
    """

    def __init__(
        self,
        agent_factory: Callable[[], Any] | None = None,
    ) -> None:
        """初始化。

        Args:
            agent_factory: 返回可运行 leaf-role agent 的 callable。
                为 None 时，_get_agent 会抛 SpecialistStartupError
                （bootstrap 必须注入 factory）。
        """
        self._agent_factory = agent_factory

    def invoke(self, request: SpecialistRequest) -> SpecialistRawResult:
        """执行 subagent 任务，返回原始输出。

        流程：_get_agent → _create_isolated_state → agent.invoke →
        _extract_output → 组装 SpecialistRawResult。

        - RecursionError → SubagentMaxStepsError。
        - 其他异常 → SpecialistCrashError。

        Args:
            request: specialist 调用请求（含 goal / context_summary / sandbox_id / max_steps）。

        Returns:
            含 raw_output 与 duration_seconds 的 SpecialistRawResult。
        """
        start = time.time()
        agent = self._get_agent()
        state = self._create_isolated_state(request)

        try:
            result = agent.invoke(
                state,
                config={"recursion_limit": request.max_steps * 2},
            )
        except RecursionError:
            from poirot.backend.agents.multiagent.exceptions import (
                SubagentMaxStepsError,
            )
            raise SubagentMaxStepsError(max_steps=request.max_steps)
        except Exception as e:
            raise SpecialistCrashError(str(e))

        raw_output = self._extract_output(result)

        return SpecialistRawResult(
            raw_output=raw_output,
            duration_seconds=time.time() - start,
        )

    def _get_agent(self) -> Any:
        """从 agent_factory 取子 Agent；未配置时抛 SpecialistStartupError。"""
        if self._agent_factory is not None:
            return self._agent_factory()
        raise SpecialistStartupError(
            "agent_factory not configured (bootstrap must inject leaf-role factory)"
        )

    def _create_isolated_state(self, request: SpecialistRequest) -> dict:
        """构造隔离的 ThreadState（只带 goal + context_summary，不继承父消息）。

        - isolated context：全新 ThreadState，不继承父 messages / observations / sources。
        - shared thread sandbox：request.sandbox_id 存在时写入 state["sandbox"]。

        Args:
            request: specialist 调用请求。

        Returns:
            新建的 ThreadState dict。
        """
        from poirot.backend.agents.state.thread_state import (
            create_initial_thread_state,
        )

        state = create_initial_thread_state(request.goal)
        if request.context_summary:
            state["metadata"]["context_summary"] = request.context_summary
        if request.sandbox_id:
            state["sandbox"] = {"sandbox_id": request.sandbox_id}
        return state

    def _extract_output(self, result: Any) -> str:
        """从 agent 结果中抽取输出文本（取最后一条消息的 content）。

        - result 为 dict 且有 messages：取最后一条的 content；无 content 则 str(last)。
        - result 为 dict 但无 messages：返回空字符串。
        - 其他类型：str(result)。

        Args:
            result: agent.invoke 的返回值。

        Returns:
            抽取出的输出文本。
        """
        if isinstance(result, dict):
            messages = result.get("messages", [])
            if messages:
                last = messages[-1]
                content = getattr(last, "content", None)
                if content is not None:
                    return str(content)
                return str(last)
            return ""
        return str(result)