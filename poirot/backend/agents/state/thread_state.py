"""初始线程状态工厂 — 构造 ThreadState 的初始值。

【整体职责】
为一次新的研究线程提供初始 ThreadState：填充 user_input，并把所有 list / dict 类
字段初始化为空容器，使图执行前 state 具备完整且可合并的基底。

【内容摘要】
- create_initial_thread_state : 由 user_input 构造初始 ThreadState。

【职责边界】
- 只负责：构造初始 state 字典。
- 不负责：状态 schema 定义（state/types）、字段合并规则（state/reducers）、
  状态的持久化与推进（runtime）。

【INVARIANT】
- 空容器基底：列表字段初始化为 []，字典字段初始化为 {}，便于 reducer 追加/合并。
- 可选字段置 None：governance / sandbox / orchestration / recalled_memories /
  memory_updates 初始为 None，表示尚未产生。
- messages 初始为空列表：供 add_messages reducer 累积对话。
- 仅填 user_input：其余非容器字段（intent / plan / research_question 等）留空，
  由图执行过程中填充。
- 返回类型为 ThreadState：仅提供部分字段，未提供字段按 NotRequired 语义缺省。
"""
from __future__ import annotations

from poirot.backend.agents.state.types import ThreadState


def create_initial_thread_state(user_input: str) -> ThreadState:
    """构造初始 ThreadState。

    Args:
        user_input: 用户输入。

    Returns:
        ThreadState: 初始状态字典，含 user_input 与空容器字段。

    说明：
        - 列表字段置 []、字典字段置 {}，便于 reducer 合并。
        - governance / sandbox / orchestration / recalled_memories / memory_updates
          置 None，表示尚未产生。
        - intent / plan / research_question 等字段不在此填，留待图执行时填充。
    """
    return {
        "messages": [],
        "user_input": user_input,
        "observations": [],
        "sources": [],
        "citations": [],
        "artifacts": [],
        "reflection_items": [],
        "errors": [],
        "metadata": {},
        "governance": None,
        "sandbox": None,
        "orchestration": None,
        "recalled_memories": None,
        "memory_updates": None,
    }