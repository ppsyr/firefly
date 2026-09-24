"""HybridRetriever — 纯 BM25 检索（无 vector/graph）。

【整体职责】
实现 Retriever Protocol，提供记忆的检索后端。
是 memory 模块唯一的检索实现，被 MemoryMiddleware（读路径）调用。
构造时全量建索引（冷启动），store 变更时增量维护。

【核心机制】
1. BM25 召回：从倒排索引算相似度。
2. retrieve 强化写回（1A）：命中后算 strength + with_strength + store.update。
3. forgotten 过滤（3B）：retrieve 时排除 metadata.forgotten=True。
4. 增量索引（5B）：构造全量建 + on_trace_* 增量维护。
5. 复合分数：score = similarity × 0.7 + strength × 0.3。

【职责边界】
- 本模块只做检索 + 强化写回，不感知遗忘判定（forget_policy 的事）。
- 纯 BM25，不依赖 vector/graph（adapters 是 Layer 6 预留）。
- forgotten 过滤在 Retriever（store 不感知）。
- 1A 强化写回在 retrieve 内部完成，caller 不负责。

【INVARIANT】
- 纯 BM25，无 vector/graph 依赖。
- forgotten 过滤在 Retriever（store 不感知）。
- 增量索引：构造全量建 + on_trace_* 增量维护。
- retrieve 强化写回：命中后 store.update，caller 不负责。
"""

from __future__ import annotations

import logging
import math
import time
from collections import defaultdict
from typing import Callable

from poirot.backend.agents.memory.memory_store import MemoryStore
from poirot.backend.agents.memory.retriever import Retriever
from poirot.backend.agents.memory.schema import MemoryTrace
from poirot.backend.agents.memory.strategies.default._constants import BM25_PARAMS
from poirot.backend.agents.memory.strategies.default.decay import EbbinghausDecayPolicy
from poirot.backend.agents.memory.types import MemoryQuery, RetrievalResult

logger = logging.getLogger(__name__)


class HybridRetriever:
    """纯 BM25 检索器（Retriever 的默认实现）。

    构造时全量建索引（5B 冷启动）+ store 变更时增量维护（5B on_trace_*）。
    retrieve 强化写回（1A）：命中后 store.update。
    forgotten 过滤（3B）：retrieve 时排除 metadata.forgotten=True。
    """

    def __init__(
        self,
        store: MemoryStore,
        decay_policy: EbbinghausDecayPolicy,
        *,
        tokenize: Callable[[str], list[str]] | None = None,
    ) -> None:
        """初始化：注入 store + decay_policy，建倒排索引，冷启动全量建索引。

        Args:
            store: 持久化后端（MarkdownFileStore）。
            decay_policy: 衰减策略（retrieve 强化时算 strength）。
            tokenize: 分词函数（None 时默认空格分词，中文需 jieba 留后续）。

        Returns:
            None。

        Raises:
            不主动抛异常；_build_index_from_store 内部异常按原逻辑传播。

        组装规则：
            1. 存 store / decay_policy 到字段。
            2. tokenize 未传入 → 用默认 _default_tokenize。
            3. 初始化倒排索引 _inverted_index（token → {trace_id: tf}）。
            4. 初始化 _trace_lengths（trace_id → token 数）。
            5. 调 _build_index_from_store() 冷启动全量建索引。
        """
        self._store = store
        self._decay_policy = decay_policy
        self._tokenize = tokenize or self._default_tokenize
        # BM25 倒排索引：token → {trace_id: tf}
        self._inverted_index: dict[str, dict[str, int]] = defaultdict(dict)
        # trace 长度（分词后 token 数，BM25 用）
        self._trace_lengths: dict[str, int] = {}
        # 全量建索引（5B 冷启动）
        self._build_index_from_store()

    def _build_index_from_store(self) -> None:
        """5B 冷启动：从 store.list_all() 全量建 BM25 索引（排除 forgotten）。

        Args:
            无。

        Returns:
            None。

        Raises:
            不主动抛异常。

        组装规则：
            1. 遍历 store.list_all() 的全部 trace。
            2. metadata.forgotten 为真 → 跳过（3B forgotten 不入索引）。
            3. 其余调 _index_trace 单条入索引。
        """
        for trace in self._store.list_all():
            if trace.metadata.get("forgotten"):  # 3B forgotten 不入索引
                continue
            self._index_trace(trace)

    def _index_trace(self, trace: MemoryTrace) -> None:
        """单条 trace 入索引（5B 增量）。

        Args:
            trace: 待入索引的 MemoryTrace。

        Returns:
            None。

        Raises:
            不主动抛异常。

        组装规则：
            1. 对 content 分词，记 trace 长度到 _trace_lengths。
            2. 统计每个 token 的词频（tf）。
            3. 把 tf 写入倒排索引 _inverted_index[token][trace.id]。
        """
        tokens = self._tokenize(trace.content)
        self._trace_lengths[trace.id] = len(tokens)
        tf: dict[str, int] = defaultdict(int)
        for token in tokens:
            tf[token] += 1
        for token, count in tf.items():
            self._inverted_index[token][trace.id] = count

    def _remove_trace_from_index(self, trace_id: str) -> None:
        """单条 trace 从索引移除（5B 增量）。

        Args:
            trace_id: 待移除的 trace id。

        Returns:
            None。

        Raises:
            不主动抛异常。

        组装规则：
            1. 从 _trace_lengths 删除该 trace。
            2. 遍历倒排索引，从每个 token 的 postings 里删该 trace。
            3. postings 变空 → 删除该 token（避免残留空条目）。
        """
        self._trace_lengths.pop(trace_id, None)
        for token in list(self._inverted_index.keys()):
            self._inverted_index[token].pop(trace_id, None)
            if not self._inverted_index[token]:
                del self._inverted_index[token]

    def retrieve(self, query: MemoryQuery) -> list[RetrievalResult]:
        """检索相关记忆，返回按 score 降序排列的结果。

        Args:
            query: MemoryQuery（text / top_k / type_filter / min_strength / metadata_filter）。

        Returns:
            RetrievalResult 列表，按复合分数降序，长度 ≤ query.top_k。

        Raises:
            不主动抛异常。

        组装规则：
            1. 取 now + 对 query.text 分词。
            2. 3B forgotten 过滤：候选 = store.list_all() 排除 forgotten。
            3. type_filter 非 None → 按 type 过滤候选。
            4. 算 avgdl（全部 trace 平均长度）+ 取 BM25 参数。
            5. 对每个候选算 BM25 相似度：
               - 相似度 ≤ 0 跳过。
               - lazy decay 算当前 strength；< query.min_strength 跳过。
               - 复合分数 score = similarity × 0.7 + strength × 0.3。
            6. 按 score 降序排序 + 截断 top_k。
            7. 1A 强化写回：命中 trace 算 strength + with_strength + store.update，
               再构造最终 RetrievalResult 返回。
        """
        now = time.time()
        query_tokens = self._tokenize(query.text)

        # 3B forgotten 过滤 + 候选集
        candidates = [t for t in self._store.list_all() if not t.metadata.get("forgotten")]
        if query.type_filter is not None:
            type_key = (
                query.type_filter.value
                if isinstance(query.type_filter, type(query.type_filter))
                else str(query.type_filter)
            )
            candidates = [t for t in candidates if t.type.value == type_key]

        # BM25 算分
        scores: list[tuple[MemoryTrace, float]] = []
        avgdl = sum(self._trace_lengths.values()) / max(1, len(self._trace_lengths))
        k1 = BM25_PARAMS["k1"]
        b = BM25_PARAMS["b"]

        for trace in candidates:
            similarity = self._bm25_score(query_tokens, trace.id, avgdl, k1, b)
            if similarity <= 0:
                continue
            # lazy decay 算 strength（1A 强化前先算当前值）
            current_strength = self._decay_policy.compute_strength(trace, now)
            if current_strength < query.min_strength:
                continue
            # 复合分数：score = similarity × 0.7 + strength × 0.3
            result = RetrievalResult.compute_score(trace, similarity, current_strength)
            scores.append((trace, result.score))

        # 按 score 降序 + top_k 截断
        scores.sort(key=lambda x: x[1], reverse=True)
        top = scores[: query.top_k]

        # 1A 强化写回：命中的 trace 调 with_strength + store.update
        results: list[RetrievalResult] = []
        for trace, score in top:
            new_strength = self._decay_policy.compute_strength(trace, now)
            strengthened = trace.with_strength(new_strength, now)
            self._store.update(strengthened)  # 1A 内部写回
            results.append(RetrievalResult.compute_score(strengthened, score, new_strength))

        return results

    def _bm25_score(
        self, query_tokens: list[str], trace_id: str, avgdl: float, k1: float, b: float
    ) -> float:
        """BM25 算分（标准公式）。

        Args:
            query_tokens: query 分词后的 token 列表。
            trace_id:     目标 trace id。
            avgdl:        全部 trace 平均长度。
            k1:           TF 饱和参数。
            b:            文档长度归一参数。

        Returns:
            BM25 分数（未归一化）。

        Raises:
            不主动抛异常。

        组装规则：
            1. 取该 trace 的长度；为 0 → 返 0.0。
            2. 遍历 query tokens：
               - 从倒排索引取 postings，tf = postings[trace_id]；为 0 跳过。
               - 算 IDF（+1 平滑防负）。
               - 算 TF 归一（BM25 标准公式）。
               - 累加 idf × tf_norm。
            3. 返回累加分数。
        """
        score = 0.0
        trace_len = self._trace_lengths.get(trace_id, 0)
        if trace_len == 0:
            return 0.0
        for token in query_tokens:
            postings = self._inverted_index.get(token, {})
            tf = postings.get(trace_id, 0)
            if tf == 0:
                continue
            # IDF（+1 平滑防负）
            df = len(postings)
            n = len(self._trace_lengths)
            idf = math.log((n - df + 0.5) / (df + 0.5) + 1)
            # TF 归一
            tf_norm = (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * trace_len / avgdl))
            score += idf * tf_norm
        return score

    @staticmethod
    def _default_tokenize(text: str) -> list[str]:
        """默认分词（空格 + 小写，中文需 Layer 3 换 jieba 留后续）。

        Args:
            text: 待分词文本。

        Returns:
            token 列表。

        Raises:
            不主动抛异常。

        组装规则：
            1. text.lower() 转小写。
            2. 按空格 split 返回。
        """
        return text.lower().split()

    # 5B 增量索引接口（供 Layer 4 store 包装调用）

    def on_trace_added(self, trace: MemoryTrace) -> None:
        """store.add 后调（5B 增量）。

        Args:
            trace: 新增的 MemoryTrace。

        Returns:
            None。

        Raises:
            不主动抛异常。

        组装规则：
            1. metadata.forgotten 为真 → 不入索引（跳过）。
            2. 否则调 _index_trace 单条入索引。
        """
        if not trace.metadata.get("forgotten"):
            self._index_trace(trace)

    def on_trace_updated(self, trace: MemoryTrace) -> None:
        """store.update 后调（5B 增量，content 变了要重建）。

        Args:
            trace: 更新后的 MemoryTrace。

        Returns:
            None。

        Raises:
            不主动抛异常。

        组装规则：
            1. 先 _remove_trace_from_index 移除旧索引。
            2. metadata.forgotten 为真 → 不重新入索引。
            3. 否则调 _index_trace 重建。
        """
        self._remove_trace_from_index(trace.id)
        if not trace.metadata.get("forgotten"):
            self._index_trace(trace)

    def on_trace_removed(self, trace_id: str) -> None:
        """store.remove 后调（5B 增量）。

        Args:
            trace_id: 被删除的 trace id。

        Returns:
            None。

        Raises:
            不主动抛异常。

        组装规则：
            1. 调 _remove_trace_from_index 从索引移除。
        """
        self._remove_trace_from_index(trace_id)