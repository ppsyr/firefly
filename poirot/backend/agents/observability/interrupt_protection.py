"""interrupt_protection - 原子任务的中断保护。

【整体职责】
某些操作（上下文压缩、证据持久化、报告生成）不允许中途被打断。本模块提供
一个线程局部的保护标志，StallDetectionMiddleware 在暂停图之前检查该标志；
若处于保护中，暂停延迟到上下文管理器退出后再执行。

【内容摘要】
- _protection_state      : 线程局部状态容器。
- is_interrupt_protected : 查询当前线程是否正在执行受保护任务。
- interrupt_protection   : 上下文管理器，标记/取消标记当前线程为受保护。

【职责边界】
- 只负责：维护并查询"当前线程是否处于中断保护"的标志。
- 不负责：停滞检测与暂停决策（StallDetectionMiddleware）、受保护任务本身的执行
  （上下文压缩 / 证据持久化 / 报告生成）、线程调度与并发控制。

【INVARIANT】
- 线程局部：标志存于 threading.local()，各线程互不影响。
- 上下文管理器语义：进入时置为 active，退出时恢复进入前的值（支持嵌套）。
- 默认不保护：未标记时 is_interrupt_protected() 返回 False。
- 保护不阻塞：本模块只提供标志，是否延迟暂停由消费方（middleware）决定。

【设计借鉴】
借鉴 hermes 的 _aux_interrupt_protection 模式。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

_protection_state = threading.local()


def is_interrupt_protected() -> bool:
    """如果当前线程正在运行受中断保护的任务，则返回True。"""
    return bool(getattr(_protection_state, "active", False))


@contextmanager
def interrupt_protection(active: bool = True) -> Iterator[None]:
    """将当前线程标记为运行受中断保护的任务。

    StallDetectionMiddleware检查中断保护（）之前暂停图形。如果受保护，则暂停被延迟到退出上下文管理器。

    Args:
        active: 是否标记为受保护；默认 True。传入 False 可显式取消标记。

    Yields:
        None: 进入保护区间；退出时恢复进入前的标记状态（支持嵌套）。
    """
    prev = getattr(_protection_state, "active", False)
    _protection_state.active = active
    try:
        yield
    finally:
        _protection_state.active = prev