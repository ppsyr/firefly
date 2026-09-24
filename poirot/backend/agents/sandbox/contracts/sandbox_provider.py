"""SandboxProvider — 沙箱生命周期管理契约。

【整体职责】
定义沙箱「获取 / 查找 / 释放 / 元信息 / 重置 / 销毁」的生命周期接口。
方案 C 生命周期层：Local / Docker 各写子类，上层（工具 / middleware / bootstrap）
只依赖本契约，不关心具体是哪种沙箱。

【内容摘要】
- SandboxProvider          : 抽象基类（ABC），定义生命周期契约。
- uses_thread_data_mounts  : 类属性，标记是否使用 thread 级数据挂载。
- needs_upload_permission_adjustment : 类属性，标记是否需要调整上传权限。
- acquire / acquire_async  : 获取（或复用）沙箱，返回 sandbox_id。
- get                      : 纯内存查找，返回 Sandbox 实例或 None。
- release                  : 释放沙箱（不一定销毁）。
- get_sandbox_info         : 返回沙箱元信息（含 sandbox_url），供 specialist 透传。
- reset                    : 清缓存，不 shutdown。
- shutdown                 : 销毁所有沙箱 + 清缓存。

【职责边界】
- 只负责：定义生命周期接口与默认行为。
- 不负责：沙箱如何创建/销毁的具体实现（子类）、命令执行（Sandbox/Runtime）、
  路径转换（Translator）、安全校验（Guard）。
- 不持有状态：本类只是契约，状态由子类持有（缓存、warm_pool 等）。

【INVARIANT】
- get(sandbox_id) 是纯内存查找，事件循环安全。
- acquire 可能阻塞（Docker 操作），async 路径用 acquire_async。
- release 不一定销毁：LocalSandboxProvider 为 no-op，DockerSandboxProvider 移入 warm_pool。
- reset 清缓存但不 shutdown；shutdown 销毁所有沙箱。
- get_sandbox_info 返回 SandboxInfo（含 sandbox_url）供 specialist 透传；
  Local 实现返回 None（无 URL 概念）。
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from poirot.backend.agents.sandbox.sandbox import Sandbox
    from poirot.backend.agents.sandbox.types import SandboxInfo


class SandboxProvider(ABC):
    """沙箱生命周期管理契约。

    方案 C 生命周期层。Local / Docker 各写子类。

    INVARIANT:
    - get(sandbox_id) 是纯内存查找，事件循环安全
    - acquire 可能阻塞（Docker 操作），async 路径用 acquire_async
    - release 不一定销毁（LocalSandboxProvider no-op，DockerSandboxProvider 移入 warm_pool）
    - reset 清缓存不 shutdown；shutdown 销毁所有沙箱
    - get_sandbox_info 返 SandboxInfo（含 sandbox_url）供 specialist 透传；Local 返 None
    """

    uses_thread_data_mounts: bool = False
    needs_upload_permission_adjustment: bool = True

    @abstractmethod
    def acquire(
        self, thread_id: str | None = None, *, user_id: str | None = None
    ) -> str:
        """获取（或复用）沙箱，返 sandbox_id。可能阻塞。"""
        ...

    async def acquire_async(
        self, thread_id: str | None = None, *, user_id: str | None = None
    ) -> str:
        """异步版本。默认 asyncio.to_thread 包同步实现。

        子类可重写以更精细控制（如 DockerSandboxProvider 重写避免阻塞事件循环）。
        """
        return await asyncio.to_thread(self.acquire, thread_id, user_id=user_id)

    @abstractmethod
    def get(self, sandbox_id: str) -> Sandbox | None:
        """纯内存查找。返 None 表示未找到。事件循环安全。"""
        ...

    @abstractmethod
    def release(self, sandbox_id: str) -> None:
        """释放沙箱（不一定销毁）。"""
        ...

    def get_sandbox_info(self, sandbox_id: str) -> SandboxInfo | None:
        """返沙箱元信息（含 sandbox_url），供 specialist 透传连接 Docker 容器。

        DockerSandboxProvider 返 SandboxInfo；LocalSandboxProvider 返 None（无 URL 概念）。
        默认返 None，Docker 子类重写。
        """
        return None

    def reset(self) -> None:
        """清缓存，不 shutdown。默认空实现。"""
        pass

    def shutdown(self) -> None:
        """销毁所有沙箱 + 清缓存。默认空实现。"""
        pass