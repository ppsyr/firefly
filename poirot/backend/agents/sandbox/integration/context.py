"""Sandbox 上下文 — 用 ContextVar 传递当前 tool_call 的 sandbox_id。

【整体职责】
在 Agent 工具调用链路上传递"当前生效的 sandbox_id"，避免显式层层传参。
通过 ContextVar 实现，天然隔离并发调用（每个协程/线程各自持有一份）。

【内容摘要】
- _sandbox_id  : ContextVar，存当前 tool_call 的 sandbox_id，缺省 None。
- set_sandbox_id: 写入入口；Stage 4 SandboxMiddleware 在 wrap_tool_call 前调用。
- get_sandbox_id: 读取入口；供工具/backend 在调用链任意位置取当前 sandbox_id。

【职责边界】
- 只负责：定义 ContextVar、提供 set / get 两个读写入口。
- 不负责：sandbox_id 的生成（由 provider 负责）、中间件的调用时机（由 middleware 负责）。
- 持有模块级 ContextVar：进程内单例，但值随上下文（协程/线程）隔离。

【INVARIANT】
- ContextVar 而非全局变量：并发 tool_call 之间互不污染。
- 缺省值为 None，表示"当前上下文未绑定沙箱"。
- set 与 get 必须成对使用：middleware 进入时 set，退出时应恢复（由调用方负责）。
"""
from __future__ import annotations

from contextvars import ContextVar

_sandbox_id: ContextVar[str | None] = ContextVar("sandbox_id", default=None)


def set_sandbox_id(sandbox_id: str | None) -> None:
    """设置当前 tool_call 的 sandbox_id。

    由 Stage 4 SandboxMiddleware 在 wrap_tool_call 前调用。
    """
    _sandbox_id.set(sandbox_id)


def get_sandbox_id() -> str | None:
    """获取当前 tool_call 的 sandbox_id；未绑定返回 None。"""
    return _sandbox_id.get()