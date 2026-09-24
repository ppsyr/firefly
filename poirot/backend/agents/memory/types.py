"""Memory 辅助类型：MemoryQuery / MemoryFilter / RetrievalResult。

【整体职责】
定义 memory 模块的辅助数据类型：检索入参、遗忘过滤条件、检索出参。
是 memory 模块的「数据底座」之一，被 retriever / store / manager 共享。

【组成】
1. MemoryQuery（frozen）    ：检索查询（retriever.retrieve 入参）。
2. MemoryFilter（frozen）   ：记忆过滤（forget_policy / store.list_by_filter 用）。
3. RetrievalResult（frozen）：检索结果（retriever.retrieve 出参）。

【职责边界】
- 本模块只定义数据模型，不含检索逻辑（检索在 retriever）。
- RetrievalResult.compute_score 是唯一的方法，只做纯计算。
- 不 import 项目内其他模块（除 schema 的类型）。

【INVARIANT】
- RetrievalResult.score 复合分数公式 score = similarity × 0.7 + strength × 0.3，
  语义相关性占主导（70%），记忆强度参与排序（30%）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from poirot.backend.agents.memory.schema import MemoryTrace, MemoryType


# ---------------------------------------------------------------------------
# 检索入参
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MemoryQuery:
    """检索查询（retriever.retrieve 入参）。

    Args:
        text:             查询文本（retriever 分词后算 BM25）。
        top_k:            返回条数上限（默认 5）。
        type_filter:      按类型过滤（None=不限）。
        min_strength:     最低强度门槛（低于此值的候选被剔除）。
        metadata_filter:  按 metadata 过滤（全匹配，空 dict=不过滤）。
    """

    text: str
    top_k: int = 5
    type_filter: MemoryType | None = None
    min_strength: float = 0.0
    metadata_filter: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 遗忘过滤
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MemoryFilter:
    """记忆过滤（遗忘策略 / store.list_by_filter 用）。

    语义：store 只按 type / max_age_hours / metadata 粗筛（内存索引），
    strength 精算由调用方（forget_policy）逐条 compute_strength。

    Args:
        type_filter:     按类型过滤（None=不限）。
        min_strength:    最低强度门槛（store 不精算，仅透传给调用方参考）。
        max_age_hours:   最大年龄（小时，None=不限）。
                         按 last_accessed（≤0 用 created_at）粗筛。
        metadata_filter: 按 metadata 过滤（全匹配，空 dict=不过滤）。
    """

    type_filter: MemoryType | None = None
    min_strength: float = 0.0
    max_age_hours: float | None = None       # 最大年龄（小时），None=不限
    metadata_filter: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 检索出参
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RetrievalResult:
    """检索结果（retriever.retrieve 出参）。

    Args:
        trace:      命中的 MemoryTrace（可能已强化：strength / access_count 更新）。
        similarity: 语义相似度 0.0~1.0（BM25 归一后）。
        strength:   当前强度（retrieve 时按需计算）。
        score:      复合分数 = similarity × 0.7 + strength × 0.3。
    """

    trace: MemoryTrace
    similarity: float                        # 语义相似度 0.0~1.0
    strength: float                          # 当前强度（retrieve 时按需计算）
    score: float                             # = similarity * 0.7 + strength * 0.3

    @classmethod
    def compute_score(
        cls, trace: MemoryTrace, similarity: float, strength: float
    ) -> "RetrievalResult":
        """构造 RetrievalResult 并算复合分数。

        复合分数公式：score = similarity × 0.7 + strength × 0.3
        （语义相关性占主导 70%，记忆强度参与排序 30%）。

        Args:
            trace:      命中的 MemoryTrace。
            similarity: 语义相似度。
            strength:   当前强度。

        Returns:
            构造完成的 RetrievalResult（含 score）。

        Raises:
            不主动抛异常。

        组装规则：
            1. score = similarity × 0.7 + strength × 0.3。
            2. 用 cls(...) 构造并返回。
        """
        score = similarity * 0.7 + strength * 0.3
        return cls(trace=trace, similarity=similarity, strength=strength, score=score)