"""L2TriggerMiddleware — L1 graph after_model 末尾轻量检查。

【整体职责】
作为 L1 graph 的 after_model 钩子：每次模型调用后，用纯数值判断检查是否
满足 L2 演化触发条件；命中则 enqueue EvolutionTask 到共享队列，由 daemon
线程消费。不修改 ThreadState，延迟 < 1ms。

【内容摘要】
- L2TriggerMiddleware : middleware 主类（after_model / aafter_model）。

【职责边界】
- 只负责：after_model 时委托 TriggerManager 判定、命中则 enqueue。
- 不负责：触发判定逻辑（TriggerManager 负责）、任务执行（worker 负责）、
  ThreadState 修改（返 None，不改）。
- 不持有重状态：只持有 trigger_manager / metrics_view / profile 引用。

【INVARIANT】
- after_model 不修改 ThreadState：始终返回 None。
- 纯数值判断：不调 LLM（委托 TriggerManager.should_trigger）。
- 延迟 < 1ms：不影响 L1 turn 性能。
- 触发判定完全委托 TriggerManager（四源 + 1h 冷却）。
- 异步版委托同步版：L2 触发是纯数值，无阻塞 IO，不需真正异步。
- EvolutionTask.task_id 从 state.metadata 的 snapshot_id / thread_id 提取；
  都没有则用 l2_<时间戳> 兜底。
- trigger_source 从 TriggerManager.last_trigger_source 取；
  None 时用 TriggerSource.PERIODIC 兜底。
- trigger_detail 从 TriggerManager.last_trigger_detail 取。
- profile 由构造注入，默认 "default"。
"""
from __future__ import annotations

import time
from typing import Any, override

from langchain.agents.middleware.types import AgentMiddleware

from poirot.backend.agents.multiagent.evolution.metrics_view import MetricsView
from poirot.backend.agents.multiagent.evolution.trigger_manager import TriggerManager
from poirot.backend.agents.multiagent.evolution.types import EvolutionTask, TriggerSource
from poirot.backend.agents.state.types import ThreadState


class L2TriggerMiddleware(AgentMiddleware):
    """L1 graph after_model 末尾轻量检查。

    - after_model 不修改 ThreadState（始终返 None）。
    - 判定委托 TriggerManager（纯数值，不调 LLM）。
    - 命中时 enqueue EvolutionTask 到 queue（daemon thread 消费）。
    - 延迟 < 1ms，不影响 L1 turn。
    """

    state_schema = ThreadState  # type: ignore[assignment]

    def __init__(
        self,
        trigger_manager: TriggerManager,
        metrics_view: MetricsView,
        profile: str = "default",
    ) -> None:
        """初始化。

        Args:
            trigger_manager: 触发管理器（委托判定）。
            metrics_view: L2 MetricsView Protocol（读 L1 指标）。
            profile: 演化 profile（per-profile 冷却 key），默认 "default"。
        """
        self._trigger_manager = trigger_manager
        self._metrics_view = metrics_view
        self._profile = profile

    @override
    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """L1 graph after_model 末尾轻量检查。

        流程：
        1. _should_trigger(state) 委托 TriggerManager 判定。
        2. 命中 → _enqueue_evolution_task(快照 ID)。
        3. 始终返回 None（不修改 ThreadState）。

        Args:
            state: ThreadState（只读）。
            runtime: LangGraph runtime（未使用）。

        Returns:
            始终 None。
        """
        if not self._should_trigger(state):
            return None
        self._enqueue_evolution_task(self._extract_snapshot_id(state))
        return None  # 不修改 state

    @override
    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """异步版委托同步（L2 触发是纯数值，无阻塞 IO）。"""
        return self.after_model(state, runtime)

    def _should_trigger(self, state: Any) -> bool:
        """纯数值判断：委托 TriggerManager.should_trigger（四源 + 1h 冷却，不调 LLM）。

        Args:
            state: ThreadState（本方法未使用，预留）。

        Returns:
            是否应触发。
        """
        return self._trigger_manager.should_trigger(
            self._metrics_view, self._profile
        )

    def _extract_snapshot_id(self, state: Any) -> str:
        """从 ThreadState 提取 snapshot_id（用作 EvolutionTask.task_id）。

        查找顺序：state.metadata.snapshot_id > state.metadata.thread_id。
        都找不到返回空串。

        Args:
            state: ThreadState。

        Returns:
            快照 ID 或空串。
        """
        if isinstance(state, dict):
            metadata = state.get("metadata") or {}
            if isinstance(metadata, dict):
                sid = metadata.get("snapshot_id") or metadata.get("thread_id")
                if isinstance(sid, str):
                    return sid
        return ""

    def _enqueue_evolution_task(self, snapshot_id: str) -> None:
        """enqueue EvolutionTask 到 queue（daemon thread 消费）。

        - task_id: snapshot_id，空则用 l2_<时间戳> 兜底。
        - trigger_source: TriggerManager.last_trigger_source，None 时用 PERIODIC 兜底。
        - trigger_detail: TriggerManager.last_trigger_detail。
        - per-profile 串行由 daemon 单线程消费保证。

        Args:
            snapshot_id: 从 state 提取的快照 ID。
        """
        trigger_source = self._trigger_manager.last_trigger_source or TriggerSource.PERIODIC
        task = EvolutionTask(
            task_id=snapshot_id or f"l2_{int(time.time())}",
            profile=self._profile,
            trigger_source=trigger_source,
            trigger_detail=self._trigger_manager.last_trigger_detail,
            timestamp=str(int(time.time())),
        )
        self._trigger_manager.enqueue(task)