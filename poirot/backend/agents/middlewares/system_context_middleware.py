"""SystemContextMiddleware — before_model 注入 system_context 元数据。

【整体职责】
在每次进入 model 之前，往 state 的 metadata 里写入一份 system_context，
内容是当前 agent 的身份标识与运行时区。下游（如 prompt 渲染、trace 审计、
系统提示拼装）可以据此拿到「我是谁、现在是什么时区」这类元信息。

【时区来源】
时区不是写死的，而是通过 _get_runtime_value 从 runtime 上读取：
- 取到则用 runtime 上的 "timezone"；
- 取不到则回退默认值 "Asia/Shanghai"。

【职责边界】
- 只负责：把 system_context 写入 state.metadata（轻量、幂等的元数据注入）。
- 不负责：消费 system_context（prompt 渲染 / trace 审计由下游负责）、
  时区配置的来源（runtime 提供）、其他 state 字段的读写。

【INVARIANT】
- 只写 metadata：不改 messages、不改其他 state 字段。
- 幂等：每次 before_model 都写入相同内容（agent 标识固定）。
- 时区来源：从 runtime 读 "timezone"，取不到回退 "Asia/Shanghai"。
- 异步转同步：abefore_model 直接调 before_model。
- agent 标识固定："Poirot deep research agent"。
"""

from __future__ import annotations

from typing import Any, override

from langchain.agents.middleware.types import AgentMiddleware
from langgraph.runtime import Runtime

from poirot.backend.agents.middlewares.run_journal_middleware import _get_runtime_value


class SystemContextMiddleware(AgentMiddleware):
    """before_model 注入 system_context 元数据（agent 身份 + 时区）。

    在每次 model 调用前，把 system_context 写入 state.metadata。
    同步 / 异步共用同一套逻辑，异步版直接转调同步版。
    """

    def before_model(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        """同步 before_model hook：向 state.metadata 注入 system_context。

        处理流程：
        1. 从 runtime 读取 "timezone"，取不到回退 "Asia/Shanghai"。
        2. 组装 system_context：
           - agent：固定标识 "Poirot deep research agent"；
           - timezone：上一步取到的时区。
        3. 以 {"metadata": {"system_context": {...}}} 形式返回，
           由框架合并进 state.metadata。

        Args:
            state:   当前 state（本 hook 未直接使用其内容）。
            runtime: LangGraph 运行时，用于读取 timezone 配置。

        Returns:
            含 metadata.system_context 的 state patch。
        """
        tz = _get_runtime_value(runtime, "timezone", "Asia/Shanghai")
        return {"metadata": {"system_context": {"agent": "Poirot deep research agent", "timezone": tz}}}

    async def abefore_model(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        return self.before_model(state, runtime)