"""Sandbox readiness polling — sync + async。

【整体职责】
提供等待沙箱就绪的轮询函数：轮询 {sandbox_url}/v1/sandbox，HTTP 200 视为就绪，
超时返回 False。sync / async 两个版本，供 provider 在创建容器后等待其可用。

【内容摘要】
- wait_for_sandbox_ready       : sync 版本；阻塞轮询直到就绪或超时。
- wait_for_sandbox_ready_async : async 版本；不阻塞事件循环，带可调 poll_interval。

【职责边界】
- 只负责：轮询 HTTP 端点判就绪 + 超时控制。
- 不负责：沙箱创建 / 销毁（provider）、HTTP 客户端生命周期（每次调用自建自销）。
- 无状态：不持有字段；httpx 缺失时抛 RuntimeError。

【INVARIANT】
- 就绪判定：GET {sandbox_url}/v1/sandbox 返回 200。
- 单次请求超时 5 秒；sync 每次轮询间隔 1 秒。
- 总超时由 timeout 参数控制，默认 60 秒。
- 所有 HTTP 异常静默吞掉——轮询期间"未就绪 / 网络抖动"都视为继续等待。
- async 版不阻塞事件循环；用 loop.time() 算 deadline，sleep 用 min(poll_interval, remaining) 避免超时后仍 sleep。
- httpx 缺失时抛 RuntimeError（延迟导入，非硬依赖）。
"""
from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]


def wait_for_sandbox_ready(sandbox_url: str, timeout: int = 60) -> bool:
    """轮询 /v1/sandbox 直到 ready 或超时。sync 版本。

    单次请求超时 5s；轮询间隔 1s；总超时由 timeout 控制。
    所有 HTTP 异常静默吞掉（视为继续等待）。
    """
    if httpx is None:
        raise RuntimeError("httpx required for readiness polling")
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = httpx.get(f"{sandbox_url}/v1/sandbox", timeout=5)
            if resp.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


async def wait_for_sandbox_ready_async(
    sandbox_url: str, timeout: int = 60, poll_interval: float = 1.0,
) -> bool:
    """轮询 /v1/sandbox 直到 ready 或超时。async 版本，不阻塞事件循环。

    用 loop.time() 算 deadline；sleep 取 min(poll_interval, remaining)——
    避免剩余时间不足 poll_interval 时仍睡满一个周期。
    """
    if httpx is None:
        raise RuntimeError("httpx required for readiness polling")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    async with httpx.AsyncClient(timeout=5) as client:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                resp = await client.get(
                    f"{sandbox_url}/v1/sandbox", timeout=min(5.0, remaining),
                )
                if resp.status_code == 200:
                    return True
            except Exception:
                pass
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(poll_interval, remaining))
    return False