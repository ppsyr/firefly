"""SummarizationMiddleware — AgentMiddleware no-op 占位（V1）。

【整体职责】
当前是一个空壳（no-op）：before_model 返回 None，不干预任何流程。
存在的目的是占好中间件位置，方便后续接入真正的摘要逻辑，可选方案：
- 复用 langchain.agents.middleware.summarization.SummarizationMiddleware；
- 或自定义摘要实现。

【为什么保留空壳】
保留接口占位，后续启用时只需替换内部实现，不改中间件挂载位置与调用约定。
"""

from __future__ import annotations

from typing import Any, override

from langchain.agents.middleware.types import AgentMiddleware
from langgraph.runtime import Runtime


class SummarizationMiddleware(AgentMiddleware):
    """摘要中间件（V1 占位）。

    当前两个 hook 都是 no-op，不做任何摘要；保留接口供后续接入。
    """

    @override
    def before_model(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        """同步 before_model：no-op，固定返回 None。

        Args:
            state:   当前 state（未使用）。
            runtime: LangGraph 运行时（未使用）。

        Returns:
            始终 None（不写 state）。
        """
        return None

    @override
    async def abefore_model(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        """异步 before_model：no-op，固定返回 None。

        Args:
            state:   当前 state（未使用）。
            runtime: LangGraph 运行时（未使用）。

        Returns:
            始终 None（不写 state）。
        """
        return None