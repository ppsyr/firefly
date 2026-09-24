"""Sandbox 生命周期收尾 — 进程退出时统一 shutdown 已注册的 provider。

【整体职责】
把 sandbox provider 的清理动作挂到进程退出钩子上，保证进程正常退出时
已创建的 provider 能被统一关闭；多次注册不重复挂 atexit handler。

【内容摘要】
- _registered_providers    : 已注册的 provider 列表，进程退出时逐个 shutdown。
- _atexit_registered       : atexit handler 是否已挂载的标记，保证只挂一次。
- register_sandbox_shutdown: 注册入口；追加 provider，首次调用时挂 atexit。
- _shutdown_all_providers  : atexit 回调；遍历 shutdown，逐个吞异常，最后清空列表。
- _reset_for_testing       : 测试隔离用；清空列表 + 重置挂载标记。

【职责边界】
- 只负责：注册 provider、进程退出时统一调 shutdown、测试隔离重置。
- 不负责：provider 的创建与具体 shutdown 实现（由各 provider 自己负责）、
  非正常退出（如 SIGKILL）的清理。
- 持有模块级状态：_registered_providers / _atexit_registered（进程内单例）。

【INVARIANT】
- atexit handler 全局只挂一次；重复调用 register 只追加 provider，不重复挂载。
- shutdown 时逐个 try/except，单个 provider 失败不影响其余 provider。
- shutdown 完成后列表清空，避免重复关闭。
- 只覆盖正常退出路径；SIGKILL 等强杀不会触发本模块逻辑。
"""
from __future__ import annotations

import atexit

from poirot.backend.agents.sandbox.contracts import SandboxProvider

_registered_providers: list[SandboxProvider] = []
_atexit_registered: bool = False


def register_sandbox_shutdown(provider: SandboxProvider) -> None:
    """注册 provider 到进程退出时的统一 shutdown 流程。

    多次调用只挂一次 atexit handler；provider 累积到 _registered_providers。
    """
    global _atexit_registered
    _registered_providers.append(provider)
    if not _atexit_registered:
        atexit.register(_shutdown_all_providers)
        _atexit_registered = True


def _shutdown_all_providers() -> None:
    """进程退出回调：逐个 shutdown 已注册 provider，吞掉异常后清空列表。"""
    for provider in _registered_providers:
        try:
            provider.shutdown()
        except Exception:
            pass
    _registered_providers.clear()


def _reset_for_testing() -> None:
    """测试隔离：清空 provider 列表 + 重置 atexit 挂载标记。"""
    global _atexit_registered
    _registered_providers.clear()
    _atexit_registered = False