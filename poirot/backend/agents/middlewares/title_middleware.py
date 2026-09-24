"""TitleMiddleware — after_agent 设置 metadata.title。

【整体职责】
在 run 结束时，为本次运行生成一个简短标题，写入 state.metadata.title。
标题来源按优先级取：
1. state["research_question"]（研究型任务的正式问题）；
2. state["user_input"]（普通对话的用户输入）；
3. 兜底字面量 "Untitled"。

标题统一截断到 60 个字符，避免过长污染 metadata / UI 展示。

【只写 metadata】
本中间件只往 metadata 里塞 title，不改 messages、不改其他 state 字段，
属于轻量、幂等的元数据写入。
"""

from __future__ import annotations

from typing import Any, override

from langchain.agents.middleware.types import AgentMiddleware
from langgraph.runtime import Runtime


class TitleMiddleware(AgentMiddleware):
    """after_agent 设置 metadata.title（取自 research_question / user_input，截断 60）。
    """

    def after_agent(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        """同步 after_agent hook：生成并写入 metadata.title。

        处理流程：
        1. 按优先级取标题来源：
           - state["research_question"]；
           - 取不到再取 state["user_input"]；
           - 都没有则用 "Untitled"。
        2. 把来源强转为 str 并截断到前 60 个字符。
        3. 以 {"metadata": {"title": ...}} 形式返回，由框架合并进 state.metadata。

        Args:
            state:   当前 state，读取 research_question / user_input。
            runtime: LangGraph 运行时（本 hook 未使用）。

        Returns:
            含 metadata.title 的 state patch。
        """
        source = state.get("research_question") or state.get("user_input") or "Untitled"
        return {"metadata": {"title": str(source)[:60]}}

    async def aafter_agent(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        return self.after_agent(state, runtime)