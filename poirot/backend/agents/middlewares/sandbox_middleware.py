"""SandboxMiddleware — Sandbox 生命周期中间件（只 async，Grill #9）。

【整体职责】
管理 sandbox 的「按需获取（lazy acquire）→ 跨轮复用 → 结束释放」生命周期，
并把 present_files 的虚拟路径落成真实产出物 + 注册到 ArtifactServer。

【INVARIANT（必须保持的不变量）】
- lazy_init 硬编码 True：没有 before_agent hook；sandbox 只有在
  sandbox 工具真正被调用时才 acquire。
- abefore_model：从 state["sandbox"] 恢复 ContextVar。
  subagent 共享父 sandbox_id（块 D1）：父已设 state["sandbox"]，
  子进程/子 agent 在这里恢复 ContextVar，awrap_tool_call 看到已设就跳过 acquire。
- awrap_tool_call：sandbox 工具首次调用时执行 acquire + set_sandbox_id，
  并用 Command 把 sandbox_id 持久化进 state。
- present_files 调用后：把 virtual path 解析成 host path，复制到
  .poirot/outputs/，写入 state.artifacts 并向 ArtifactServer 注册。
- aafter_agent release：release 不销毁（LocalSandboxProvider 为 no-op）。
- Sandbox 中间件在中间件列表外层；ToolCall 在内层 catch SandboxError（Grill #9）。
- 非 sandbox 工具（web_search 等）不触发 acquire。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.runtime import Runtime
from langgraph.types import Command

from poirot.backend.agents.agent_tools.available import SANDBOX_TOOL_NAMES
from poirot.backend.agents.artifacts.server import ArtifactServer
from poirot.backend.agents.sandbox.contracts import SandboxProvider
from poirot.backend.agents.sandbox.integration.context import (
    get_sandbox_id,
    set_sandbox_id,
)
from poirot.backend.agents.state.types import Artifact

logger = logging.getLogger(__name__)

_VIRTUAL_PREFIX = "/mnt/poirot/user-data/"  # present_files 允许的虚拟路径前缀


class SandboxMiddleware(AgentMiddleware):
    """Sandbox 生命周期中间件（只 async，Grill #9）。

    INVARIANT:
    - lazy_init 硬编码 True：无 before_agent，sandbox 工具被调用时 acquire。
    - abefore_model：从 state["sandbox"] 恢复 ContextVar
      （subagent 共享父 sandbox_id，块 D1）。
    - awrap_tool_call：sandbox 工具首次调用时 acquire + set_sandbox_id
      + Command 持久化。
    - present_files 调用后：把 virtual path 写入 state.artifacts
      + 向 ArtifactServer 注册。
    - aafter_agent release：release 不销毁（LocalSandboxProvider no-op）。
    - Sandbox 在中间件列表外层；ToolCall 在内层 catch SandboxError（Grill #9）。
    - 非 sandbox 工具（web_search 等）不触发 acquire。
    """

    def __init__(
        self,
        provider: SandboxProvider,
        artifact_server: ArtifactServer | None = None,
        sandbox_root: str | None = None,
    ) -> None:
        """初始化。

        Args:
            provider:       sandbox 提供者（acquire / release / get）。
            artifact_server: 产出物 HTTP 下载服务，可为 None（不注册）。
            sandbox_root:   host 侧 sandbox 根目录，可为 None（走 sandbox translator）。
        """
        self._provider = provider
        self._artifact_server = artifact_server
        self._sandbox_root = sandbox_root

    async def abefore_model(
        self, state: dict[str, Any], runtime: Runtime
    ) -> dict[str, Any] | None:
        """恢复 ContextVar from state["sandbox"]（subagent 共享父 sandbox_id）。

        处理流程：
        1. 若 ContextVar 已设（get_sandbox_id() 非 None）直接返回 None，
           不覆盖（lead 同进程多轮的场景）。
        2. 否则读 state["sandbox"]，若其含 sandbox_id，
           用 set_sandbox_id 恢复到 ContextVar。

        语义：
        - lead 首次调用时 state["sandbox"] 为 None，不恢复，
          走 awrap_tool_call 的 acquire 流程。
        - subagent 继承父 sandbox_id（state["sandbox"] 已设），
          此处恢复 ContextVar，awrap_tool_call 看到已设 → 跳过 acquire
          → 复用父 Sandbox。

        Args:
            state:   当前 state，读取 sandbox.sandbox_id。
            runtime: LangGraph 运行时（本 hook 未使用）。

        Returns:
            始终 None（只恢复 ContextVar，不改 state）。
        """
        if get_sandbox_id() is not None:
            return None  # ContextVar 已设（lead 同进程多轮），不覆盖
        sandbox_state = state.get("sandbox")
        if isinstance(sandbox_state, dict) and sandbox_state.get("sandbox_id"):
            set_sandbox_id(sandbox_state["sandbox_id"])
        return None

    @staticmethod
    def _emit_sandbox_acquired(sandbox_id: str) -> None:
        """向 stream 推一条 sandbox_acquired custom 事件（实时，不等 values-mode）。

        通过 langgraph.config.get_stream_writer 取 writer，发送
        {"type": "sandbox_update", "content": sandbox_id}。
        任何异常只记 debug 日志，不影响主流程。

        Args:
            sandbox_id: 刚 acquire 到的 sandbox id。
        """
        try:
            from langgraph.config import get_stream_writer
            writer = get_stream_writer()
            writer({
                "type": "sandbox_update",
                "content": sandbox_id,
            })
        except Exception:
            logger.debug("Failed to emit sandbox_update custom event", exc_info=True)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """异步 wrap_tool_call：sandbox 工具按需 acquire + present_files 落产出物。

        处理流程：
        1. 取 tool_name；非 SANDBOX_TOOL_NAMES 直接透传 handler（不触发 acquire）。
        2. 读 ContextVar sandbox_id，判断是否首次 acquire。
        3. 首次 acquire：
           - 从 request.runtime.config.configurable 取 thread_id；
             缺失则抛 SandboxRuntimeError（acquire 必须有 thread_id）。
           - provider.acquire(thread_id) 得到 sandbox_id；
           - set_sandbox_id(sandbox_id) 写 ContextVar；
           - _emit_sandbox_acquired(sandbox_id) 推 custom 事件。
        4. 调 handler(request) 拿 result。
        5. 若 tool_name == "present_files" 且有 artifact_server：
           - _register_artifacts 解析/复制/注册，得到 urls；
           - 若 urls 非空且 result 是 ToolMessage，
             在原 content 后追加 "\\n\\nDownload:\\n  url..."。
        6. 若本次是首次 acquire 且 result 是 ToolMessage：
           返回 Command(update={"sandbox": {"sandbox_id": ...}, "messages": [result]})，
           把 sandbox_id 持久化进 state。
        7. 否则返回 result。

        Args:
            request: 工具调用请求（含 tool_call / runtime）。
            handler: 下游异步处理函数。

        Returns:
            工具调用结果，可能是原 result、追加下载链接的 ToolMessage，
            或首次 acquire 时的 Command。
        """
        tool_name = request.tool_call.get("name", "")

        if tool_name not in SANDBOX_TOOL_NAMES:
            return await handler(request)

        sandbox_id = get_sandbox_id()
        first_acquire = sandbox_id is None

        if first_acquire:
            config = getattr(request.runtime, "config", None) or {}
            configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
            thread_id = configurable.get("thread_id")
            if thread_id is None:
                raise SandboxRuntimeError(
                    "thread_id missing in runtime config (sandbox acquire requires thread_id)"
                )
            sandbox_id = self._provider.acquire(thread_id)
            set_sandbox_id(sandbox_id)
            self._emit_sandbox_acquired(sandbox_id)

        result = await handler(request)

        # present_files: register artifacts
        if tool_name == "present_files" and self._artifact_server is not None:
            urls = self._register_artifacts(request, sandbox_id)
            if urls and isinstance(result, ToolMessage):
                url_text = "\n".join(f"  {u}" for u in urls)
                content = result.content if isinstance(result.content, str) else str(result.content)
                result = ToolMessage(
                    content=f"{content}\n\nDownload:\n{url_text}",
                    tool_call_id=result.tool_call_id,
                    name=result.name,
                )

        if first_acquire and isinstance(result, ToolMessage):
            return Command(
                update={
                    "sandbox": {"sandbox_id": sandbox_id},
                    "messages": [result],
                }
            )
        return result

    def _register_artifacts(self, request: ToolCallRequest, sandbox_id: str) -> list[str]:
        """解析 present_files 的 virtual paths，复制到 .poirot/outputs/，注册到 server。

        借鉴 deer-flow：deer-flow 用 router 从原位置 serve；Poirot CLI 场景
        更适合复制到固定目录 .poirot/outputs/——用户总知道去哪里找产出物，
        任何格式都支持。同时注册到 ArtifactServer 供 HTTP 下载。

        处理流程：
        1. 从 request.tool_call["args"] 取 paths：
           - dict → args["paths"]；
           - str  → 视为无 paths；
           - list → 直接用；
           - 其他 → 空。
        2. 取 sandbox 对象（用于解析 host 路径）；确保 .poirot/outputs/ 存在。
        3. 逐个 virtual path 处理：
           - 非 str 或不在 _VIRTUAL_PREFIX 下 → 记 warning 跳过。
           - filename = 去掉前缀后的相对路径。
           - 解析 host_path：优先 sandbox.get_host_path(vp)；
             异常或无 sandbox 时退回 f"{sandbox_root}/{sandbox_id}/{filename}"
             （sandbox_root 为 None 则跳过）。
           - 复制到 outputs_dir / filename（copy2，保留元数据），
             失败仅 warning。
           - 若 artifact_server 存在，register(sandbox_id, filename, dest)
             得到 URL 并加入 urls。
           - 若复制失败但原始 host_path 存在且已注册，
             再用原始 host_path 注册一次（去重）。
        4. 返回 urls。

        Args:
            request:    工具调用请求，读取 args.paths。
            sandbox_id: 当前 sandbox id。

        Returns:
            已注册的下载 URL 列表（可能为空）。
        """
        import shutil
        from pathlib import Path

        from poirot.backend.app.bootstrap import _PROJECT_ROOT

        args = request.tool_call.get("args", {})
        if isinstance(args, dict):
            paths = args.get("paths", [])
        elif isinstance(args, str):
            paths = []
        else:
            paths = args if isinstance(args, list) else []

        # 获取 sandbox 对象用于解析 host 路径
        sandbox = self._provider.get(sandbox_id)
        outputs_dir = _PROJECT_ROOT / ".poirot" / "outputs"
        outputs_dir.mkdir(parents=True, exist_ok=True)

        urls: list[str] = []
        for vp in paths:
            if not isinstance(vp, str) or not vp.startswith(_VIRTUAL_PREFIX):
                logger.warning(
                    f"present_files: skipping '{vp}' — must be under {_VIRTUAL_PREFIX}"
                )
                continue
            filename = vp[len(_VIRTUAL_PREFIX):].lstrip("/")

            # 1. 解析 host 路径（通过 sandbox translator）
            try:
                if sandbox is not None:
                    host_path = sandbox.get_host_path(vp)
                else:
                    host_path = f"{self._sandbox_root}/{sandbox_id}/{filename}" if self._sandbox_root else None
            except Exception as exc:
                logger.warning(f"present_files: failed to resolve host path for '{vp}': {exc}")
                host_path = f"{self._sandbox_root}/{sandbox_id}/{filename}" if self._sandbox_root else None

            if not host_path:
                continue

            # 2. 复制到 .poirot/outputs/（固定产出物目录）
            dest = outputs_dir / filename
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(host_path, dest)
                logger.info(f"Artifact copied: {host_path} → {dest}")
            except Exception as exc:
                logger.warning(f"present_files: copy failed '{host_path}' → '{dest}': {exc}")

            # 3. 注册到 ArtifactServer（供 HTTP 下载）
            if self._artifact_server is not None:
                url = self._artifact_server.register(sandbox_id, filename, str(dest))
                urls.append(url)
                logger.info(f"Artifact registered: {url}")

            # 4. 同时注册原始 host_path（如复制失败，至少原始路径可下载）
            if self._artifact_server is not None and not dest.exists() and Path(host_path).exists():
                url = self._artifact_server.register(sandbox_id, filename, host_path)
                if url not in urls:
                    urls.append(url)

        return urls

    async def aafter_agent(
        self, state: dict[str, Any], runtime: Runtime
    ) -> None:
        """异步 after_agent：释放本次 run 的 sandbox。

        从 state["sandbox"] 取 sandbox_id，存在则调 provider.release。
        LocalSandboxProvider 的 release 为 no-op（不销毁），
        远端 provider 可据此真正回收资源。

        Args:
            state:   当前 state，读取 sandbox.sandbox_id。
            runtime: LangGraph 运行时（本 hook 未使用）。

        Returns:
            始终 None（只做释放，不改 state）。
        """
        sandbox_state = state.get("sandbox")
        if sandbox_state and sandbox_state.get("sandbox_id"):
            self._provider.release(sandbox_state["sandbox_id"])