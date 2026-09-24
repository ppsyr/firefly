"""SandboxBinder Protocol — specialist 与沙箱的绑定契约。

【整体职责】
定义"specialist 调用前如何绑定沙箱"的契约：接收 specialist 名与 thread sandbox_id，
返回一个绑定结果 BoundSandbox。语义是"共享 thread sandbox"——复用 lead agent 的
sandbox_id，不创建新沙箱。

【内容摘要】
- BoundSandbox(frozen)     : 绑定结果，含 sandbox_id + specialist_name。
- SandboxBinder(Protocol)  : 绑定契约，仅一个 bind() 方法。

【职责边界】
- 只负责：定义绑定契约（方法签名 + 语义）。
- 不负责：沙箱创建/销毁（sandbox provider 负责）、路径翻译（translator 负责）、
  安全校验（guard 负责）、实际执行（runtime 负责）。
- 不持有状态：Protocol 无实现，纯接口。

【INVARIANT】
- shared thread sandbox：lead + self-copy + specialist 共用同一个 sandbox_id，
  不创建新沙箱。
- 共享而非物理隔离：specialist 与 lead 看到同一份文件系统。
- per-specialist-call 生命周期：每次 specialist 调用绑定一次，BoundSandbox 记录
  specialist_name 以标识归属。
- bind 只复用传入的 sandbox_id，不做任何变换。
- 实现示例：PerSubagentBinder——复用 lead agent 的 thread sandbox_id。
"""
from __future__ import annotations

from dataclasses import dataclass

from typing import Protocol


@dataclass(frozen=True)
class BoundSandbox:
    """specialist 沙箱绑定结果。

    - sandbox_id 与 lead agent 同一 thread sandbox_id（共享，非物理隔离）。
    - specialist_name 标识哪个 specialist 绑定（per-specialist-call 生命周期）。
    """

    sandbox_id: str
    specialist_name: str


class SandboxBinder(Protocol):
    """沙箱绑定契约（specialist 调用前绑定 shared thread sandbox）。

    实现示例：PerSubagentBinder——复用 lead agent thread sandbox_id，
    不创建新 sandbox。
    """

    def bind(self, specialist_name: str, sandbox_id: str) -> BoundSandbox:
        """绑定 specialist 与 sandbox，返回 BoundSandbox。

        语义：shared thread sandbox——复用传入的 sandbox_id，不创建新沙箱。

        Args:
            specialist_name: 要绑定的 specialist 名。
            sandbox_id: lead agent 的 thread sandbox_id。

        Returns:
            含 sandbox_id 与 specialist_name 的 BoundSandbox。
        """
        ...