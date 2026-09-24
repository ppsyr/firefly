"""Retriever Protocol — 检索策略契约。

【整体职责】
定义检索后端必须提供的接口。
是 memory 模块的 7 个 Protocol 之一，约束 retriever 实现（默认 HybridRetriever）。

【组合模型（叠加非互斥）】
按 config 叠加，可同时启用：
- BM25（总在，从 Markdown truth 索引）。
- optional VectorStore（config.vector_store 启用时，derived 检索加速）。
- optional GraphStore（config.graph_store 启用时，derived 关联扩散）。

四种形态：纯 BM25 / BM25+Vector / BM25+Graph / BM25+Vector+Graph。

【retrieve 强化（在实现内完成）】
- 检索时按需计算 strength（Ebbinghaus 公式，lazy decay）。
- 命中 trace 自动 access_count+1 + last_accessed 更新（frozen 语义：替换）。
- 复合分数：score = similarity × 0.7 + strength × 0.3。

【职责边界】
- 本模块只定义接口签名，零实现（Protocol 纯契约）。
- 强化写回在实现里（Layer 3），不在 Protocol。
- 默认实现：HybridRetriever（strategies/default/retriever.py）。
- 可替换：VectorRetriever / GraphRetriever / TemporalRetriever（adapters/，Layer 6）。
- adapter 加载失败 → no-op 跳过该路召回 + log warning（保证系统不崩）。

【INVARIANT】
- Protocol 纯契约零实现。
- retrieve 强化在实现里（Layer 3）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from poirot.backend.agents.memory.types import MemoryQuery, RetrievalResult


@runtime_checkable
class Retriever(Protocol):
    """检索策略协议。

    默认实现 HybridRetriever（strategies/default/retriever.py，Layer 3），
    可替换 VectorRetriever / GraphRetriever / TemporalRetriever（adapters/，Layer 6）。
    """

    def retrieve(self, query: MemoryQuery) -> list[RetrievalResult]:
        """检索相关记忆，返回按 score 降序排列的结果。

        Args:
            query: MemoryQuery（text / top_k / type_filter / min_strength /
                   metadata_filter）。

        Returns:
            RetrievalResult 列表，按复合分数降序，长度 ≤ query.top_k。

        Raises:
            不主动抛异常；实现内部异常按实现原逻辑传播。

        组装规则：
            1. 按 query 检索相关记忆。
            2. 复合分数：score = similarity × 0.7 + strength × 0.3。
            3. 命中 trace 自动强化（lazy decay + access_count+1 + last_accessed 更新）。
            4. 按 score 降序返回，截断到 top_k。
        """
        ...