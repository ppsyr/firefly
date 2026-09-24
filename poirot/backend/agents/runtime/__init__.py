"""Runtime lifecycle package — 运行时生命周期模块。

【整体职责】
围绕"一次运行（run）"提供全生命周期支持：运行记录的数据契约、运行创建与状态推进的
调度中心、LangGraph state 持久化、以及运行期共享的上下文容器。是上层（app / AppRuntime）
与下层（graph / journal）之间的运行时枢纽。

【内容摘要】
- run_record     : 运行记录数据契约（RunRecord / RunStatus），定义一次运行的数据形态。
- run_manager    : 运行管理器（RunManager），创建 run、推进状态、持久化记录的核心调度中心。
- checkpointer   : LangGraph state 持久化单例（InMemorySaver），作为 create_agent 编译参数。
- run_context    : 运行上下文（RunContext），运行期跨组件共享的容器与路径派生。

【职责边界】
- 只负责：运行的数据形态、创建与状态推进、state 持久化、运行期上下文承载。
- 不负责：执行依赖的装配（leader_agent / capability_registry 由上层 AppRuntime 持有）、
  日志写入实现（journal 模块）、配置结构定义（config 模块）、graph 编译（create_agent）。

【四层结构】
- 数据结构：run_record（一次运行长什么样）。
- 核心管理：run_manager（怎么管理运行，装配 RunContext / RunRecord / RunJournal）。
- 持久化  ：checkpointer（LangGraph state 存/取）。
- 上下文  ：run_context（运行期在各组件间传递的上下文）。
"""
from poirot.backend.agents.runtime.checkpointer import get_checkpointer
from poirot.backend.agents.runtime.run_context import RunContext
from poirot.backend.agents.runtime.run_manager import RunManager
from poirot.backend.agents.runtime.run_record import RunRecord, RunStatus

__all__ = [
    "get_checkpointer",
    "RunContext",
    "RunManager",
    "RunRecord",
    "RunStatus",
]