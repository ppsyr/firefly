"""默认 MemoryProvider 主入口（组合 store + retriever + manager）。

【整体职责】
把 memory 的各个组件（store / retriever / manager / decay / forget）组装成一个
可直接使用的 DefaultMemoryProvider，是主链装配动作的落地实现。

bootstrap._load_memory_provider 调用本模块的 build_default_provider()，
本模块内部从 config 实例化 store / retriever（未注入时），组装 manager。

【组成】
1. DefaultMemoryProvider（frozen dataclass）：
   provider 的默认实现，组合 store + retriever + manager 三组件。
   构造即就绪；shutdown 走 hasattr duck-type 委托 store/retriever。

2. build_default_provider(...)：
   装配函数。支持部分注入（Layer 2 测试 mock）和完整实例化（Layer 3 从 config 建）。

【职责边界】
- 本模块只负责「组装」，不负责装配的触发时机（由 bootstrap 决定何时调用）。
- store / retriever / decay / forget 支持由调用方注入（测试用），
  未注入时从 config 实例化真实实现。
- journal 由调用方注入（bootstrap 通过 _make_journal_callback 传入），
  本模块只透传给 manager，不做事件定义。

【INVARIANT】
- DefaultMemoryProvider frozen dataclass，三组件构造时注入，运行时不可变。
- shutdown duck-type：委托 store/retriever（hasattr 检查）。
- store / retriever / journal 由调用方注入（Layer 2 测试 mock，Layer 3 后真实实例）。
- 组装顺序：先 decay / forget，再 store，再 retriever，最后 manager（后者依赖前者）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from poirot.backend.agents.memory.config import get_memory_config
from poirot.backend.agents.memory.memory_manager import MemoryManager
from poirot.backend.agents.memory.memory_provider import MemoryProvider
from poirot.backend.agents.memory.memory_store import MemoryStore
from poirot.backend.agents.memory.retriever import Retriever
from poirot.backend.agents.memory.strategies.default.decay import EbbinghausDecayPolicy
from poirot.backend.agents.memory.strategies.default.forget import CompositeForgetPolicy
from poirot.backend.agents.memory.strategies.default.manager import DefaultMemoryManager
from poirot.backend.agents.memory.strategies.default.retriever import HybridRetriever
from poirot.backend.agents.memory.strategies.default.store import MarkdownFileStore


# ---------------------------------------------------------------------------
# Provider 实现
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DefaultMemoryProvider:
    """默认 MemoryProvider 实现（组合 store + retriever + manager）。

    frozen dataclass，三组件构造时注入，运行时不可变。
    Lifecycle：构造即就绪，shutdown 走 hasattr duck-type（委托给 store/retriever）。
    """

    _store: MemoryStore
    _retriever: Retriever
    _manager: MemoryManager

    def store(self) -> MemoryStore:
        """返回持久化后端。"""
        return self._store

    def retriever(self) -> Retriever:
        """返回检索后端。"""
        return self._retriever

    def manager(self) -> MemoryManager:
        """返回四操作编排。"""
        return self._manager

    def shutdown(self) -> None:
        """可选 shutdown：委托给 store / retriever（若有）。

        hasattr duck-type：
        - MarkdownFileStore 无 shutdown（纯文件 IO）→ 跳过。
        - SQLiteShadowStore / VectorStore 有 shutdown（关连接 / unload model）→ 调用。
        """
        if hasattr(self._store, "shutdown"):
            self._store.shutdown()
        if hasattr(self._retriever, "shutdown"):
            self._retriever.shutdown()


# ---------------------------------------------------------------------------
# 装配函数
# ---------------------------------------------------------------------------

def build_default_provider(
    *,
    store: MemoryStore | None = None,
    retriever: Retriever | None = None,
    decay_policy: EbbinghausDecayPolicy | None = None,
    forget_policy: CompositeForgetPolicy | None = None,
    journal: Callable[[str, dict], None] | None = None,
) -> MemoryProvider:
    """组装默认 MemoryProvider。

    Args:
        store: 持久化后端（None 时从 config 实例化 MarkdownFileStore）。
        retriever: 检索后端（None 时从 store + decay_policy 实例化 HybridRetriever）。
        decay_policy: 衰减策略（None 时默认 EbbinghausDecayPolicy）。
        forget_policy: 遗忘策略（None 时默认 CompositeForgetPolicy，内部用 decay）。
        journal: 事件回调（traceability B，None 时不发事件；Layer 4 注入 RunJournal）。

    Returns:
        DefaultMemoryProvider（组合三组件）。

    Raises:
        不主动抛异常；各组件构造 / config 读取的异常按原逻辑传播。

    组装规则：
        1. 读 config = get_memory_config()（取 storage_path 等装配参数）。
        2. decay = decay_policy 或 默认 EbbinghausDecayPolicy()。
        3. forget = forget_policy 或 默认 CompositeForgetPolicy(decay)。
        4. store 未注入 → 从 config 实例化 MarkdownFileStore(config.storage_path)。
        5. retriever 未注入 → 用 store + decay 实例化 HybridRetriever。
        6. manager = DefaultMemoryManager(store, decay_policy=decay,
           forget_policy=forget, journal=journal)。
        7. 返回 DefaultMemoryProvider(_store, _retriever, _manager)。

    分层说明：
        - Layer 2：store / retriever 由调用方注入（测试 mock）。
        - Layer 3：store / retriever 未注入时从 config 实例化（真实实现）。
        - Layer 4：bootstrap 调本函数并注入 journal（RunJournal callback）。
    """
    config = get_memory_config()
    decay = decay_policy or EbbinghausDecayPolicy()
    forget = forget_policy or CompositeForgetPolicy(decay)

    # Layer 3 完整实例化（store/retriever 未注入时从 config 建）
    if store is None:
        store = MarkdownFileStore(config.storage_path)
    if retriever is None:
        retriever = HybridRetriever(store, decay)

    manager = DefaultMemoryManager(
        store=store,
        decay_policy=decay,
        forget_policy=forget,
        journal=journal,  # traceability B 透传
    )
    return DefaultMemoryProvider(
        _store=store,
        _retriever=retriever,
        _manager=manager,
    )