"""MemoryStore Protocol — 记忆持久化契约。

【整体职责】
定义持久化后端必须提供的接口。
是 memory 模块的 7 个 Protocol 之一，约束 store 实现（默认 MarkdownFileStore）。

【组成】
8 个方法：
- add / get / update / batch_update / remove  ← 写操作
- list_by_type / list_by_filter / list_all    ← 读操作

【职责边界】
- 本模块只定义接口签名，零实现（Protocol 纯契约）。
- 不感知 forgotten（过滤在 Retriever）、不精算 strength（精算在调用方）。
- 默认实现：MarkdownFileStore（strategies/default/store.py，Layer 3）。
- 可替换：SQLiteStore / VectorStore / GraphStore（adapters/，Layer 6）。

【INVARIANT】
- Protocol 纯契约零实现，可 mock 可替换。
- 写操作遵循 frozen 语义（替换，非原地修改）。
- add 冲突抛 MemoryConflictError；update / batch_update 找不到抛 MemoryNotFoundError。
- remove 幂等（不存在静默）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from poirot.backend.agents.memory.schema import MemoryTrace, MemoryType
from poirot.backend.agents.memory.types import MemoryFilter


@runtime_checkable
class MemoryStore(Protocol):
    """记忆持久化协议。

    默认实现 MarkdownFileStore（strategies/default/store.py，Layer 3），
    可替换 SQLiteStore / VectorStore / GraphStore（adapters/，Layer 6）。

    方法分组：
    - 写操作：add / get / update / batch_update / remove。
    - 读操作：list_by_type / list_by_filter / list_all。
    """

    def add(self, trace: MemoryTrace) -> None:
        """新增记忆。

        Args:
            trace: 待新增的 MemoryTrace。

        Returns:
            None。

        Raises:
            MemoryConflictError: trace.id 已存在。

        组装规则：
            1. trace.id 已存在 → 抛 MemoryConflictError。
            2. 否则写入（实现决定落盘方式）。
        """
        ...

    def get(self, trace_id: str) -> MemoryTrace | None:
        """按 id 取记忆。

        Args:
            trace_id: 记忆 id。

        Returns:
            对应 MemoryTrace；不存在返 None。

        Raises:
            不主动抛异常。

        组装规则：
            1. 直接查（实现决定数据来源）。
            2. 不存在返 None。
        """
        ...

    def update(self, trace: MemoryTrace) -> None:
        """更新记忆（frozen 语义：替换）。

        Args:
            trace: 替换后的 MemoryTrace（id 必须已存在）。

        Returns:
            None。

        Raises:
            MemoryNotFoundError: trace.id 不存在。

        组装规则：
            1. trace.id 不存在 → 抛 MemoryNotFoundError。
            2. 否则替换（实现决定落盘方式）。
        """
        ...

    def batch_update(self, traces: list[MemoryTrace]) -> None:
        """批量更新（frozen 语义：替换）。

        用于 consolidate 标记旧 trace forgotten 批量提交
        （避免 N 次 update 的 O(N²)，MarkdownFileStore 一次全量重写）。

        Args:
            traces: 待批量替换的 MemoryTrace 列表。

        Returns:
            None。

        Raises:
            MemoryNotFoundError: traces 中任一 id 不存在（全成功或全失败）。

        组装规则：
            1. 先全量校验：任一 id 不存在 → 抛 MemoryNotFoundError（不修改）。
            2. 全部合法 → 逐条替换。
            3. 实现决定提交方式（一次提交 / 逐条提交）。
        """
        ...

    def remove(self, trace_id: str) -> None:
        """删除记忆。

        Args:
            trace_id: 待删除的记忆 id。

        Returns:
            None。

        Raises:
            不主动抛异常。

        组装规则：
            1. trace_id 存在 → 删除。
            2. 不存在 → 静默返回（幂等）。
        """
        ...

    def list_by_type(self, type: MemoryType) -> list[MemoryTrace]:
        """按类型列出。

        Args:
            type: 记忆类型（MemoryType）。

        Returns:
            该类型下的所有 MemoryTrace 列表。

        Raises:
            不主动抛异常。

        组装规则：
            1. 遍历全部，过滤 type 匹配的 trace 返回。
        """
        ...

    def list_by_filter(self, filter: MemoryFilter) -> list[MemoryTrace]:
        """按过滤器列出（遗忘策略用）。

        Args:
            filter: MemoryFilter（type_filter / min_strength / max_age_hours / metadata_filter）。

        Returns:
            过滤后的 MemoryTrace 列表。

        Raises:
            不主动抛异常。

        组装规则：
            1. store 只做粗筛（type / max_age_hours / metadata）。
            2. strength 精算由调用方（forget_policy）逐条 compute_strength。
        """
        ...

    def list_all(self) -> list[MemoryTrace]:
        """列出全部。

        Args:
            无。

        Returns:
            所有 MemoryTrace 的列表。

        Raises:
            不主动抛异常。

        组装规则：
            1. 返回全部 trace。
        """
        ...