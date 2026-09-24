"""Memory 配置：MemoryConfig + STARTUP_ONLY_FIELDS + 全局单例。

【整体职责】
集中定义长期记忆的配置模型与全局单例访问点，是主链三大件（bootstrap /
MemoryMiddleware / MemoryConsolidationMiddleware）以及 strategies 内部
（store / retriever / manager / decay / forget）共同的配置来源。

【组成】
1. MemoryConfig（frozen dataclass）：
   长期记忆配置模型，单实例不可变；runtime 切换走「构造新实例 + 整替」。
   组合模型（叠加非互斥）：
   - Markdown truth（总在）    ：storage_path 指向持久化根，记忆必须落盘。
   - vector_store（可选叠加）  ：VectorStore adapter 路径，空=不启用，
                                 作为 derived 检索加速索引。
   - graph_store（可选叠加）   ：GraphStore adapter 路径，空=不启用，
                                 作为 derived 关联扩散索引。
   - vector_store + graph_store 可同时启用（三叠加：Markdown + Vector + Graph）。
2. STARTUP_ONLY_FIELDS（frozenset）：
   变更需重启的字段集合，仅 4 个：use / storage_path / vector_store / graph_store。
   与 sandbox 同款「STARTUP_ONLY」语义。
3. 全局单例：
   - _memory_config + _config_lock  ：模块级配置单例 + 互斥锁。
   - get_memory_config()            ：每次调用取最新（runtime 可切）。
   - set_memory_config(config)      ：整替全局引用（frozen 语义）。

【职责边界】
- 本模块只负责「配置模型定义 + 单例存取」，不负责配置如何被消费：
  真正的读取发生在 Provider / Middleware / Retriever / strategies 内部，
  它们每次从 get_memory_config() 取最新，不在方法内缓存 config。
- STARTUP_ONLY 字段的「生效与否」不由本模块强制，而是由约定保证：
  换这些字段需重启进程（走 bootstrap 重建 Provider + adapter + derived index），
  set_memory_config 对这些字段的变更不会自动重建运行时组件。
- frozen + dict 内部可变的矛盾解决方式：dict 视为不可变；
  改 decay 参数 = 构造新 MemoryConfig + 整替（不做局部 dict 修改）。

【INVARIANT】
- runtime 可切：enable_recall / enable_extract / token_budget / decay / forget /
  phase2 通过 set_memory_config() 整替生效；Provider / Middleware / Retriever
  不缓存 config，每次从 get_memory_config() 取最新。
- STARTUP_ONLY 仅 4 字段：use / storage_path / vector_store / graph_store
  换需重启（重建 Provider/adapter + 重建 derived index）；其余 runtime 可切。
- frozen + dict 内部可变矛盾解决：dict 视为不可变，改 decay 参数 = 构造新
  MemoryConfig + 整替。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# 配置模型
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MemoryConfig:
    """长期记忆配置（frozen，单实例不可变；runtime 切走整替）。

    组合模型（叠加非互斥）：
    - Markdown truth（总在）：storage_path 指向持久化根，记忆必须落盘。
    - vector_store（可选叠加）：VectorStore adapter 路径，空=不启用，
      作为 derived 检索加速索引。
    - graph_store（可选叠加）：GraphStore adapter 路径，空=不启用，
      作为 derived 关联扩散索引。
    - vector_store + graph_store 可同时启用（三叠加：Markdown + Vector + Graph）。

    字段语义：
    - use: 主 MemoryProvider 实现类路径（空=禁用记忆；默认 "default" 指
      strategies/default/）。
    - storage_path: Markdown 持久化根目录（相对路径锚定 _PROJECT_ROOT）。
    - enable_recall / enable_extract: Phase 1 开关（runtime 可切）。
    - token_budget: 召回注入 token 预算上限（runtime 可切）。
    - decay / forget: 衰减与遗忘参数（runtime 可切，整替非局部改）。
    - phase2: Phase 2 记忆管理 cron 配置（runtime 可切）。
    - vector_store / graph_store: 可选叠加 adapter（空=不启用）。

    字段变更语义：
    - STARTUP_ONLY 字段（use / storage_path / vector_store / graph_store）：
      变更需重启，见模块级 STARTUP_ONLY_FIELDS。
    - 其余字段（enable_recall / enable_extract / token_budget / decay /
      forget / phase2）：runtime 可切，走 set_memory_config() 整替。
    """

    use: str = ""                              # 主 Provider 实现类，空=禁用；默认 "default"
    storage_path: str = ".poirot/memory"       # Markdown 持久化根（truth source，总在）
    enable_recall: bool = True                 # before_model 召回（Phase 1）
    enable_extract: bool = False               # after_model 实时抽取（默认关，走 Phase 2）
    token_budget: int = 2000                   # 召回注入 token 上限
    decay: dict[str, Any] = field(default_factory=lambda: {
        "episodic": {"base_strength": 0.7, "decay_rate": 0.1},
        "semantic": {"base_strength": 0.8, "decay_rate": 0.02},
        "procedural": {"base_strength": 0.9, "decay_rate": 0.005},
    })
    forget: dict[str, Any] = field(default_factory=lambda: {
        "strength_threshold": 0.1,
        "ttl_hours": 720,   # 30 天
    })
    phase2: dict[str, Any] = field(default_factory=lambda: {
        "enabled": False,
        "trigger_every_n_turns": 10,
        "trigger_on_session_end": True,
    })
    vector_store: str = ""                     # 可选 Vector adapter（空=不启用，叠加模式）
    graph_store: str = ""                      # 可选 Graph adapter（空=不启用，叠加模式）


# ---------------------------------------------------------------------------
# STARTUP_ONLY 字段（变更需重启）
# ---------------------------------------------------------------------------
# 4 字段均涉及「换后端实现类 / 换存储路径 / 开关叠加 adapter」，
# 换任一需重建实例 + 重建 derived index。
#
# 其余字段（enable_recall / enable_extract / token_budget / decay / forget / phase2）
# runtime 可切，走 set_memory_config() 整替。
# ---------------------------------------------------------------------------
STARTUP_ONLY_FIELDS = frozenset({
    "use",           # 换主 Provider 实现类
    "storage_path",  # 换 truth 根目录（需迁移文件）
    "vector_store",  # 开/关 Vector adapter（需重建 Vector index）
    "graph_store",   # 开/关 Graph adapter（需重建 Graph index）
})


# ---------------------------------------------------------------------------
# 全局配置单例（runtime 可切）
# ---------------------------------------------------------------------------
# Provider / Middleware / Retriever 方法内不缓存 config，
# 每次从 get_memory_config() 取最新。
#
# runtime 切 = set_memory_config(new_config) 替换全局引用（老引用 GC）。
#
# frozen + dict 内部可变矛盾解决：dict 视为不可变，
# 改 decay 参数 = 构造新 MemoryConfig + 整替。
# ---------------------------------------------------------------------------
_memory_config: MemoryConfig = MemoryConfig()
_config_lock = threading.Lock()


def get_memory_config() -> MemoryConfig:
    """取当前 MemoryConfig（每次调用取最新，runtime 可切）。

    Args:
        无。

    Returns:
        当前全局 MemoryConfig 实例。

    Raises:
        不主动抛异常。

    组装规则：
        1. 加 _config_lock。
        2. 返回 _memory_config（模块级单例）。
    """
    with _config_lock:
        return _memory_config


def set_memory_config(config: MemoryConfig) -> None:
    """整替 MemoryConfig（runtime 切，frozen 语义）。

    STARTUP_ONLY 字段（use / storage_path / vector_store / graph_store）
    变更不生效，需重启进程（走 bootstrap 重建 Provider）。
    其余字段立即生效。

    Args:
        config: 新的 MemoryConfig 实例（frozen，整体替换）。

    Returns:
        None。

    Raises:
        不主动抛异常。

    组装规则：
        1. 加 _config_lock。
        2. 把 _memory_config 整替为传入的 config（老引用由 GC 回收）。
    """
    global _memory_config
    with _config_lock:
        _memory_config = config