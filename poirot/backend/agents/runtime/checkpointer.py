"""Checkpointer 工厂 — LangGraph graph 状态持久化单例。

【整体职责】
提供 LangGraph graph 状态持久化的全局单例 checkpointer。作为 create_agent(checkpointer=...)
的编译参数交给 LangGraph，由其内部处理持久化；thread_id 经 config.configurable 透传，
自动按 thread 存取 state。

【内容摘要】
- get_checkpointer   : 返回全局 checkpointer 单例（MVP: InMemorySaver）。
- reset_checkpointer : 重置单例，强制下次创建新实例（测试用）。

【职责边界】
- 只负责：创建并提供 checkpointer 单例。
- 不负责：state 的读写逻辑（LangGraph 内部处理，无 hook）、thread 的组织
  （由 config.configurable 透传 thread_id）、graph 的编译（create_agent）。

【INVARIANT】
- 单例：_cp 全局唯一，首次调用创建，后续复用。
- MVP 用 InMemorySaver：进程内、非持久化；进阶可升 SqliteSaver（config 驱动，接口不变）。
- 跨模式累积：模式切换重建 graph 时复用同一单例 + 同一 thread_id，使 state 跨模式保留。
- 无 hook：持久化由 LangGraph 内部完成，本模块不介入读写。

【设计借鉴】
借鉴 deer-flow 的 runtime/checkpointer/provider.py。
"""
from __future__ import annotations

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Checkpointer

_cp: Checkpointer | None = None


def get_checkpointer() -> Checkpointer:
    """返回全局 checkpointer 单例。MVP: InMemorySaver（进程内，非持久化）。

    首次调用创建 InMemorySaver，后续返回同一实例。模式切换重建 graph 时
    复用同一单例 + 同一 thread_id，使 state 跨模式累积保留。

    Returns:
        Checkpointer: 全局唯一的 checkpointer 实例。
    """
    global _cp
    if _cp is None:
        _cp = InMemorySaver()
    return _cp


def reset_checkpointer() -> None:
    """重置单例，强制下次 get_checkpointer() 创建新实例。测试用。"""
    global _cp
    _cp = None