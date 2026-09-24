"""MemoryConsolidationMiddleware — aafter_model 每 N 轮非阻塞触发记忆沉淀（L5）。

【整体职责】
在每次模型调用结束后（after_model），按「用户轮次」计数；每到 N 的倍数
就构造一个沉淀任务（MemoryTask）丢给后台 worker 异步处理，本中间件立即
返回、不阻塞主流程。

【内容摘要】
- MemoryConsolidationMiddleware : 自动沉淀中间件，aafter_model 每 N 轮 submit 任务。
- MemoryConsolidationMiddleware.abefore_model : no-op（沉淀只在 after_model）。
- MemoryConsolidationMiddleware.aafter_model : 轮次计数 + submit 任务。

【职责边界】
- 只负责：何时触发沉淀、交什么数据给 worker（轮次计数 + 截取 messages + submit）。
- 不负责：真正的沉淀逻辑（worker 侧异步完成）、记忆召回（MemoryMiddleware）、
  记忆存储与检索（memory 子系统）、worker 的生命周期（bootstrap）。

【挂载位置】
MemoryMiddleware 之后（召回在前，沉淀在后）。

【设计要点】
- 不阻塞：worker.submit(task) 后立即 return None，真正处理由 worker 异步完成。
- 不抽取全量历史：只取最近 N*2 条 messages（N 轮 ≈ 2N 条 message），
  避免 token 爆炸。
- 轮次计数只算 HumanMessage（用户轮次），tool / AI / memory_recall 等
  不计入。

【INVARIANT（必须保持的不变量）】
- 仅 aafter_model 触发（abefore_model 为 no-op）。
- turn_count % N == 0 才触发（非 N 的倍数不 submit）。
- submit 后立即 return None（worker 异步处理）。
- messages 截断到最近 N*2 条（避免 token 爆炸）。
- N 至少为 1（max(1, ...) 防除零）。
- thread_id 解析多级回退：runtime.config → state → "unknown"。
- 不写 state（submit 后立即返回，结果由 worker 异步落库）。
"""
from __future__ import annotations

import logging
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime

from poirot.backend.agents.memory.worker import MemoryTask, MemoryWorker
from poirot.backend.agents.state.types import ThreadState

logger = logging.getLogger(__name__)


class MemoryConsolidationMiddleware(AgentMiddleware):
    """自动沉淀中间件：aafter_model 每 N 轮丢任务到 worker。

    挂载位置：MemoryMiddleware 后（召回在前，沉淀在后）。
    不阻塞：submit 后立即 return None。

    Attributes:
        state_schema: 状态 schema（ThreadState）。
        _worker: 后台记忆处理 worker。
        _n: 触发间隔（用户轮次数）。
    """

    state_schema = ThreadState  # type: ignore[assignment]

    def __init__(
        self,
        worker: MemoryWorker,
        *,
        trigger_every_n_turns: int = 10,
    ) -> None:
        """初始化。

        Args:
            worker: 后台记忆处理 worker，提供 submit(task)。
            trigger_every_n_turns: 每多少个用户轮次触发一次沉淀，默认 10；
                内部用 max(1, ...) 保证至少为 1，避免除零。
        """
        self._worker = worker
        self._n = max(1, trigger_every_n_turns)

    async def abefore_model(
        self, state: ThreadState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """before_model：no-op。

        沉淀只在 after_model 做，这里固定返回 None。

        Args:
            state: 当前 ThreadState（未使用）。
            runtime: LangGraph 运行时（未使用）。

        Returns:
            dict[str, Any] | None: 始终 None。
        """
        return None  # 沉淀只在 after_model

    async def aafter_model(
        self, state: ThreadState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """after_model：按用户轮次计数，每 N 轮 submit 一个沉淀任务。

        处理流程：
        1. 取 state.messages；为空则走后续判断自然返回。
        2. 统计用户轮次：只数 HumanMessage（tool / AI / memory_recall 不计）。
        3. 若 user_turn_count == 0 或不是 N 的倍数 → 返回 None（不触发）。
        4. 取最近 N*2 条 messages（N 轮 ≈ 2N 条，避免 token 爆炸）。
        5. 解析 thread_id：runtime.config["configurable"]["thread_id"] 优先，
           回退 state["thread_id"]，再回退字面量 "unknown"；
           取 runtime config 抛异常时退回 state["thread_id"]。
        6. 构造 MemoryTask(thread_id, messages=recent, turn_count=user_turn_count)，
           调 worker.submit(task) 异步处理。
        7. 记 debug 日志后返回 None。

        注意：本中间件不做实际沉淀，只负责「何时触发 + 交什么数据」，
        真正的处理在 worker 侧异步完成。

        Args:
            state: 当前 ThreadState，读取 messages / thread_id。
            runtime: LangGraph 运行时，读取 config.configurable.thread_id。

        Returns:
            dict[str, Any] | None: 始终 None（不写 state，submit 后立即返回）。
        """
        messages = state.get("messages", []) or []
        # turn_count 只算 HumanMessage(用户轮次),不算 tool/AI/memory_recall
        user_turn_count = sum(1 for m in messages if isinstance(m, HumanMessage))
        if user_turn_count == 0 or user_turn_count % self._n != 0:
            return None

        # 取最近 N*2 条（N 轮 ≈ 2N messages，避免 token 爆炸）
        recent = messages[-(self._n * 2):]
        # thread_id 优先从 runtime config 取(fallback state → "unknown")
        thread_id = "unknown"
        try:
            config = getattr(runtime, "config", None) or {}
            configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
            thread_id = configurable.get("thread_id") or state.get("thread_id") or "unknown"
        except Exception:
            thread_id = state.get("thread_id", "unknown")
        task = MemoryTask(
            thread_id=thread_id, messages=recent, turn_count=user_turn_count,
        )
        self._worker.submit(task)
        logger.debug(
            f"MemoryConsolidationMiddleware submitted task: "
            f"thread={thread_id} turn={user_turn_count}"
        )
        return None