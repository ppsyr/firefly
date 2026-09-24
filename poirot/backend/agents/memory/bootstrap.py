"""Memory bootstrap lifecycle — get / reset / shutdown / set + 反射加载 + journal 注入。

【整体职责】
Memory 运行时主链的「装配与生命周期」节点。
把长期记忆的 provider + worker 从零构建出来，并管住它们的生死：
- 会话开始前：懒加载装配 provider（读 config → build → 装饰 store）。
- 会话进行中：provider 缓存复用，供 MemoryMiddleware（读）与
  MemoryConsolidationMiddleware（写）取用。
- 会话结束后：shutdown 关闭 provider / worker。

上层（app / middleware）一律通过本模块拿 provider，不直接持有全局单例；
worker（L5）的启停也集中在此。装配动作本身下沉到
strategies/default/strategy.py（build_default_provider）。

【主链位置】
bootstrap（本模块，装配）
    → MemoryMiddleware（abefore_model 读：召回 + 注入）
    → MemoryConsolidationMiddleware（aafter_model 写：submit 沉淀任务）
    → MemoryWorker（异步 encode / consolidate）
    → MarkdownFileStore（落盘）
    → _wrap_store 同步 HybridRetriever 索引 → 下一轮读得到新记忆（闭环）

【组成】
1. Provider lifecycle 4 函数（对外主接口）：
   - get_memory_provider()      ：懒加载 + 双检锁 + 反射 config.use。
                                  首次访问触发 _load_memory_provider，
                                  之后缓存于 _memory_provider 复用。
                                  config.use=="" 时返回 None（记忆禁用，主链短路）。
   - reset_memory_provider()    ：清缓存不关闭（测试用）。
   - shutdown_memory_provider() ：关闭 + 清缓存
                                  （hasattr duck-type 委托 store/retriever）。
   - set_memory_provider(p)     ：注入（测试用）。
2. Worker lifecycle（L5）：
   - start_memory_worker(manager, llm) ：启动 daemon 线程 worker，幂等；
                                         llm 由调用方注入（避免反向依赖 app）。
   - shutdown_memory_worker(timeout)   ：drain + 关闭 worker。
   - get_memory_worker()               ：取当前 worker 实例（可能为 None）。
3. 内部辅助：
   - _load_memory_provider ：调 build_default_provider（L3 完整版）+ journal 注入
                             + _wrap_store 装饰（5B 增量索引闭环）。
   - _make_journal_callback：RunJournal 可用时返回 lambda，不可用时返回 None
                             （静默降级，不阻断装配）。
   - _wrap_store           ：装饰器模式，把 store 变更同步到 retriever 索引
                             （主链读写一致的关键粘合剂）。

【挂载/调用顺序（固定）】
get_memory_provider
    → （首次）_load_memory_provider
        → _make_journal_callback                 # journal 注入（可降级 None）
        → build_default_provider(journal=...)    # 真正装配 store/retriever/manager/policies
        → _wrap_store(provider.store(), provider.retriever())  # 5B 索引闭环
    → 返回 provider（缓存于 _memory_provider）

【职责边界】
- 本模块只负责 provider 的「获取 / 重置 / 关闭 / 注入」与 worker 的「启停」，
  不实现 memory 的具体存储、检索、衰减、遗忘逻辑
  （那些在 strategies/default/ 下的 store / retriever / manager / decay / forget）。
- 装配动作本身不在本模块：build_default_provider 在
  strategies/default/strategy.py，本模块只负责调用它 + 注入 journal + 装饰 store。
- journal 注入通过 _make_journal_callback 间接完成；RunJournal 不可用时静默降级
  （返回 None），不阻断 provider 构建（主链对 journal 弱依赖）。
- 5B 增量索引同步由 _wrap_store 以「运行时方法替换」实现，不改 store 类本身；
  这是主链「写路径 → 读路径」的闭环点。

【INVARIANT】
- 懒加载双检锁：get_memory_provider / start_memory_worker 均线程安全。
- shutdown duck-type：hasattr(provider, "shutdown") 委托 store/retriever；
  MarkdownFileStore 不实现 shutdown，SQLiteShadowStore / VectorStore 实现。
- config.use="" 返 None（记忆禁用）。
- runtime 可切：provider 不缓存 config，每次从 get_memory_config() 取最新
  （本模块只在装配时读一次，runtime 切换由 strategies 内部每次取最新保证）。
- import 防火墙：bootstrap.py 不 import app（journal 延迟导入且可降级）。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from poirot.backend.agents.memory.config import get_memory_config

logger = logging.getLogger(__name__)

_provider_lock = threading.Lock()
_memory_provider: Any = None


# ---------------------------------------------------------------------------
# Provider lifecycle（对外主接口 4 函数）
# ---------------------------------------------------------------------------

def get_memory_provider() -> Any:
    """懒加载 + 双检锁 + 反射 config.use。None 时返 None（记忆禁用）。

    主链入口：MemoryMiddleware / MemoryConsolidationMiddleware 通过它拿 provider。
    第一次检查无锁（快速路径），第二次检查有锁（防并发重复构建）。

    Args:
        无。

    Returns:
        memory provider 实例；config.use 为假时返回 None（记忆禁用，主链短路）。

    Raises:
        不主动抛异常；_load_memory_provider 内部异常按原逻辑传播。

    组装规则：
        1. 先做无锁快速路径检查（_memory_provider 非 None 直接返回，缓存复用）。
        2. 进入 _provider_lock 后再查一次（双检锁，防并发重复构建）。
        3. 读 config = get_memory_config()：
           - config.use 为假 → 返回 None（记忆禁用，不装配）。
           - 否则调 _load_memory_provider(config) 构建并缓存到 _memory_provider，
             返回 provider。
    """
    global _memory_provider
    if _memory_provider is not None:
        return _memory_provider
    with _provider_lock:
        if _memory_provider is not None:
            return _memory_provider
        config = get_memory_config()
        if not config.use:
            return None  # 记忆禁用
        provider = _load_memory_provider(config)
        _memory_provider = provider
        return provider


def reset_memory_provider() -> None:
    """清缓存不关闭（测试用）。

    与 shutdown 的区别：不调 provider.shutdown()，便于测试中替换/复用底层资源。

    组装规则：
        1. 加 _provider_lock。
        2. 把 _memory_provider 置 None（不关闭底层 store/retriever）。
    """
    global _memory_provider
    with _provider_lock:
        _memory_provider = None


def shutdown_memory_provider() -> None:
    """关闭 + 清缓存（hasattr duck-type 委托 store/retriever）。

    主链出口：会话结束 / 进程退出时调用，释放 store/retriever 持有的资源
    （文件句柄、SQLite 连接、向量模型等）。

    组装规则：
        1. 加 _provider_lock。
        2. 若 _memory_provider 非 None：
           - hasattr(provider, "shutdown") 为真 → 调 provider.shutdown()
             （DefaultMemoryProvider 内部再 hasattr 委托 store/retriever）。
           - 否则跳过（MarkdownFileStore 不实现 shutdown，纯文件 IO）。
        3. _memory_provider 置 None。
    """
    global _memory_provider
    with _provider_lock:
        if _memory_provider is not None:
            if hasattr(_memory_provider, "shutdown"):
                _memory_provider.shutdown()
            _memory_provider = None


def set_memory_provider(provider: Any) -> None:
    """注入（测试用）。

    用于单测 / 集成测试替换真实 provider（如注入 mock provider）。

    组装规则：
        1. 加 _provider_lock。
        2. 直接把 _memory_provider 设为传入的 provider。
    """
    global _memory_provider
    with _provider_lock:
        _memory_provider = provider


# ---------------------------------------------------------------------------
# L5 worker lifecycle（块 C3）
# ---------------------------------------------------------------------------
# worker 是主链「写路径」的执行体：
# MemoryConsolidationMiddleware.aafter_model → worker.submit(task) → worker 异步处理。
# 生命周期与 provider 解耦（各自独立的锁与实例），因为 worker 依赖 llm，
# 而 provider 不依赖 llm。
# ---------------------------------------------------------------------------
_worker_lock = threading.Lock()
_memory_worker: Any = None


def start_memory_worker(manager: Any, llm: Any) -> Any:
    """启动 memory worker（daemon 线程），返回 worker 实例。

    主链「写路径」的宿主：由装配层（app bootstrap）调用，把 worker 交给
    MemoryConsolidationMiddleware 使用。

    Args:
        manager: memory manager 实例，供 worker 调度记忆操作
                 （encode / consolidate）。
        llm:     语言模型对象，由调用方注入（避免 worker 反向依赖 app）。

    Returns:
        MemoryWorker 实例（已启动）；已启动时返回既有实例（幂等）。

    Raises:
        不主动抛异常；MemoryWorker 构造 / start 的异常按原逻辑传播。

    组装规则：
        1. 先做无锁快速路径检查（_memory_worker 非 None 直接返回，幂等）。
        2. 进入 _worker_lock 后再查一次（双检锁，防并发重复启动）。
        3. 从 memory.worker 延迟导入 MemoryWorker（避免模块级强依赖）。
        4. 构造 worker = MemoryWorker(manager=manager, llm=llm) 并 worker.start()
           （worker.start 内部起 daemon 线程）。
        5. 缓存到 _memory_worker 并返回。
    """
    global _memory_worker
    if _memory_worker is not None:
        return _memory_worker
    from poirot.backend.agents.memory.worker import MemoryWorker
    with _worker_lock:
        if _memory_worker is not None:
            return _memory_worker
        worker = MemoryWorker(manager=manager, llm=llm)
        worker.start()
        _memory_worker = worker
        return worker


def shutdown_memory_worker(timeout: float = 5.0) -> None:
    """drain + 关闭 worker。

    主链「写路径」的收尾：会话结束 / 进程退出时调用，
    等 worker 把队列里剩余任务处理完（或超时）后关闭线程。

    Args:
        timeout: 等待 worker drain 的超时秒数，默认 5.0。

    Returns:
        None。

    组装规则：
        1. 加 _worker_lock。
        2. _memory_worker 为 None → 直接返回（幂等）。
        3. 调 _memory_worker.shutdown(timeout=timeout)
           （内部 set stop event + join 线程）。
        4. _memory_worker 置 None。
    """
    global _memory_worker
    with _worker_lock:
        if _memory_worker is None:
            return
        _memory_worker.shutdown(timeout=timeout)
        _memory_worker = None


def get_memory_worker() -> Any:
    """取当前 worker 实例（可能为 None）。

    供上层检查 worker 是否已启动，或做额外编排。

    组装规则：
        1. 加 _worker_lock。
        2. 返回 _memory_worker（可能为 None，表示未启动）。
    """
    with _worker_lock:
        return _memory_worker


# ---------------------------------------------------------------------------
# 内部辅助（装配细节）
# ---------------------------------------------------------------------------

def _load_memory_provider(config: Any) -> Any:
    """反射加载 + build_default_provider + journal 注入 + store 装饰。

    主链装配的核心步骤：把契约 + 实现 + config 拼成可用的 provider。

    L3 完整版：build_default_provider（store/retriever 从 config 实例化）。
    journal 注入：_make_journal_callback（RunJournal 可用时返回 lambda，
                  不可用时返回 None，静默降级）。
    _wrap_store（5B 增量索引）在 Batch 3 加入，是主链读写闭环的关键。

    Args:
        config: memory 配置对象（本函数当前未直接使用其字段，
                实际参数通过 build_default_provider 内部读取，
                保证 runtime 可切：strategies 内部每次取最新 config）。

    Returns:
        构建完成的 memory provider 实例（store/retriever 已就绪，store 已被装饰）。

    Raises:
        不主动抛异常；build_default_provider 内部异常按原逻辑传播。

    组装规则：
        1. 从 strategies.default.strategy 延迟导入 build_default_provider
           （避免模块级强依赖 + 打破潜在循环 import）。
        2. journal = _make_journal_callback()（可能为 None）。
        3. provider = build_default_provider(journal=journal)
           （内部装配 store / retriever / manager / decay / forget）。
        4. _wrap_store(provider.store(), provider.retriever())
           # 5B 增量索引触发：store 变更 → retriever 索引同步。
        5. 返回 provider。
    """
    from poirot.backend.agents.memory.strategies.default.strategy import (
        build_default_provider,
    )

    journal = _make_journal_callback()
    provider = build_default_provider(journal=journal)
    _wrap_store(provider.store(), provider.retriever())  # 5B 增量索引触发
    return provider


def _make_journal_callback() -> Any:
    """Layer 4 注入 RunJournal（L2 manager emit memory.* 事件）。

    主链对 journal 是弱依赖：拿不到就静默降级，不影响 provider 装配。
    RunJournal 可用时返回 lambda（调 journal.append(event, payload)）；
    不可用时返回 None（不发事件）。

    Args:
        无。

    Returns:
        callable 或 None：
        - RunJournal 可用 → lambda(event, payload): journal.append(event, payload)
        - RunJournal 不可用 / 取到 None / 导入异常 → None。

    Raises:
        不向外抛异常；导入与获取异常被捕获并记录 debug 日志（静默降级）。

    组装规则：
        1. try 内从 agents.journal 延迟导入 get_run_journal
           （避免强依赖 + 打破潜在循环 import）。
        2. journal = get_run_journal()：
           - journal is None → 返回 None（不发事件）。
           - 否则返回 lambda(event, payload): journal.append(event, payload)。
        3. except 捕获任意异常 → 记录 debug 日志，返回 None（静默降级）。
    """
    try:
        from poirot.backend.agents.journal import get_run_journal

        journal = get_run_journal()
        if journal is None:
            return None
        return lambda event, payload: journal.append(event, payload)
    except Exception as exc:
        logger.debug("RunJournal unavailable, memory journal events disabled: %s", exc)
        return None


def _wrap_store(store: Any, retriever: Any) -> None:
    """装饰器模式：包装 store.add/update/batch_update/remove 后调 retriever.on_trace_*。

    主链读写闭环的关键粘合剂：
    - 写路径：worker → manager.encode/consolidate → store.add/update/batch_update
    - 读路径：MemoryMiddleware → retriever.retrieve（依赖 BM25 增量索引）
    - 本函数保证写路径的 store 变更同步到读路径的 retriever 索引（5B）。

    5B 增量索引触发：store 变更后 retriever 索引同步更新。
    运行时方法替换，不改 store 类。

    Args:
        store:     memory store 实例（其 add/update/batch_update/remove 将被替换）。
        retriever: memory retriever 实例（提供 on_trace_added/updated/removed）。

    Returns:
        None（原地替换 store 的四个方法）。

    Raises:
        不主动抛异常；被包装方法内部异常按原逻辑传播。

    组装规则：
        1. 保存原始方法引用：original_add / original_update /
           original_batch_update / original_remove。
        2. 定义 wrapped_add / wrapped_update / wrapped_batch_update / wrapped_remove：
           - 先调原方法（真正落盘）。
           - 再调 retriever 对应钩子同步索引：
             add          → on_trace_added
             update       → on_trace_updated
             batch_update → 逐条 on_trace_updated
             remove       → on_trace_removed
        3. 把 store 的四个方法替换为 wrapped_*（装饰器模式，不改类）。
    """
    original_add = store.add
    original_update = store.update
    original_batch_update = store.batch_update
    original_remove = store.remove

    def wrapped_add(trace: Any) -> None:
        original_add(trace)
        retriever.on_trace_added(trace)

    def wrapped_update(trace: Any) -> None:
        original_update(trace)
        retriever.on_trace_updated(trace)

    def wrapped_batch_update(traces: list) -> None:
        original_batch_update(traces)
        for t in traces:
            retriever.on_trace_updated(t)

    def wrapped_remove(trace_id: str) -> None:
        original_remove(trace_id)
        retriever.on_trace_removed(trace_id)

    store.add = wrapped_add
    store.update = wrapped_update
    store.batch_update = wrapped_batch_update
    store.remove = wrapped_remove