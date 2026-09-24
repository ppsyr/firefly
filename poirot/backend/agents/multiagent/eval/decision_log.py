"""L3 DecisionLog — 跨 run specialist 协作 lessons 累积。

【整体职责】
定义决策日志的读写门面：DecisionLogWriter 提供异步写入（fire-and-forget，不阻塞
L1 turn）；DecisionLogReader 提供读取最近 N 条 lesson 与归档过期记录。
持久化由 _DecisionLogStore 契约承担（L1 MultiAgentMetricsStore 实现）。

【内容摘要】
- _DecisionLogStore(Protocol) : 持久化契约（save_decision_log / get_decision_logs / archive_decision_logs）。
- DecisionLogWriter           : 异步写入口（ThreadPoolExecutor max_workers=1）。
- DecisionLogReader           : 读入口（最近 N 条 lesson + 归档）。

【职责边界】
- 只负责：定义持久化契约、提供异步写与读门面。
- 不负责：实际 SQLite 读写（L1 MultiAgentMetricsStore 负责）、
  lesson 如何被消费（L2 EvolutionMutator 负责）。
- Writer 持有 executor（用于异步），Reader 无状态。

【INVARIANT】
- 异步写 fire-and-forget：write_async 提交后立即返回，不阻塞调用方。
- 串行写：ThreadPoolExecutor max_workers=1——避免并发写冲突。
- 不直接注入 prompt：lesson 作为 L2 EvolutionMutator 的输入样本（类似 failure cases）。
- 归档不删除数据：超期记录移到 archive 表 + 删除主表记录，archive 表保留。
- 依赖 _DecisionLogStore Protocol：L1 MultiAgentMetricsStore 实现此契约。
- DecisionLogRecord 来自 eval/types.py（不重复定义）。
- shutdown(wait=True)：进程退出时等待 pending 写完成。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

from poirot.backend.agents.multiagent.eval.types import DecisionLogRecord
from poirot.backend.agents.multiagent.evolution.types import FailureCategory


class _DecisionLogStore(Protocol):
    """决策日志持久化契约。

    由 L1 MultiAgentMetricsStore 实现。
    """

    def save_decision_log(self, record: DecisionLogRecord) -> None: ...

    def get_decision_logs(
        self,
        specialist_name: str,
        failure_category: FailureCategory | None,
        limit: int,
    ) -> list[DecisionLogRecord]: ...

    def archive_decision_logs(self, retention_days: int) -> int: ...


class DecisionLogWriter:
    """异步写决策日志（fire-and-forget，不阻塞 L1 turn）。

    内部调 _DecisionLogStore.save_decision_log。
    ThreadPoolExecutor max_workers=1——串行写避免并发冲突。
    L1 tool handler 调 specialist 后调 write_async。
    """

    def __init__(self, store: _DecisionLogStore) -> None:
        """初始化。注入持久化 store，建单线程 executor。"""
        self._store = store
        self._executor = ThreadPoolExecutor(max_workers=1)

    def write_async(self, record: DecisionLogRecord) -> None:
        """异步写（fire-and-forget，不阻塞调用方）。"""
        self._executor.submit(self._store.save_decision_log, record)

    def shutdown(self) -> None:
        """关闭 executor（进程退出时调，等待 pending 写完成）。"""
        self._executor.shutdown(wait=True)


class DecisionLogReader:
    """读决策日志（L2 EvolutionMutator 演化时查最近 N 条 lesson）。

    - get_recent_lessons：按 specialist_name + failure_category 过滤 + limit。
    - archive_expired：超期记录移到 archive 表 + 删除主表（不删除数据）。
    """

    def __init__(self, store: _DecisionLogStore) -> None:
        """初始化。注入持久化 store。"""
        self._store = store

    def get_recent_lessons(
        self,
        specialist_name: str,
        failure_category: FailureCategory,
        limit: int = 5,
    ) -> list[DecisionLogRecord]:
        """查最近 N 条 lesson（作 L2 EvolutionMutator 输入样本，不进 prompt）。

        Args:
            specialist_name: 目标 specialist 名。
            failure_category: 失败分类过滤。
            limit: 最多返回条数，默认 5。

        Returns:
            DecisionLogRecord 列表。
        """
        return self._store.get_decision_logs(specialist_name, failure_category, limit)

    def archive_expired(self, retention_days: int = 90) -> int:
        """归档超期记录：移到 archive 表 + 删除主表。

        Args:
            retention_days: 保留天数，默认 90。

        Returns:
            归档条数。
        """
        return self._store.archive_decision_logs(retention_days)