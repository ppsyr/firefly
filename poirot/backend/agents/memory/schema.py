"""MemoryTrace — 记忆原子单元（frozen dataclass）。

【整体职责】
定义记忆的数据模型：记忆类型、关联、操作日志、记忆痕迹本身。
是 memory 模块的「数据底座」，被 schema 消费方（store / retriever / manager /
中间件 / worker）全部共享。

【组成】
1. MemoryType（Enum）    ：记忆类型（认知科学映射）。
2. Association（frozen） ：记忆关联（扩散激活用）。
3. OperationLog（frozen）：操作日志条目（traceability，debug 用）。
4. MemoryTrace（frozen） ：记忆痕迹本身，模块的核心数据类。

【职责边界】
- 本模块只定义数据模型，不含任何逻辑（除 with_strength / with_operation 两个派生方法）。
- 不 import 项目内其他模块（除标准库），是纯数据层。
- 所有「修改」都通过 replace() 派生新实例，不原地修改（frozen 语义）。

【INVARIANT】
- MemoryTrace 不可变：strength 等可变字段通过 with_strength() / with_operation()
  创建新实例替换（类似 skill version DAG 的 is_active 指针）。
- operation_log 上限 20 条 FIFO；retrieve 不记（高频，强化在 strength / access_count
  已体现）；actor 字段预留 turn_id（Layer 4 Middleware 注入）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# 记忆类型
# ---------------------------------------------------------------------------

class MemoryType(str, Enum):
    """记忆类型（认知科学映射）。

    三类记忆的衰减特性不同（由 _constants.DECAY_PARAMS 定义具体参数）：
    - EPISODIC  ：事件记忆，衰减快，需反复检索强化。
    - SEMANTIC  ：语义记忆，衰减慢，提炼后的稳定知识。
    - PROCEDURAL：过程记忆，几乎不衰减，习得的技能（skill 系统管理）。
    """

    EPISODIC = "episodic"        # 事件记忆：衰减快，需反复检索强化
    SEMANTIC = "semantic"        # 语义记忆：衰减慢，提炼后的稳定知识
    PROCEDURAL = "procedural"    # 过程记忆：几乎不衰减，习得的技能（skill 系统管理）


# ---------------------------------------------------------------------------
# 记忆关联
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Association:
    """记忆关联（扩散激活用）。

    语义：两条记忆之间的有向连接。
    用途：retrieve 命中一条记忆时，可沿 associations 扩散激活相关记忆（Layer 6 图检索）。

    Args:
        target_id: 目标记忆的 id。
        strength:  关联强度 0.0~1.0（默认 0.5）。
        type:      关联类型（related / causal / temporal / contrast 等，默认 "related"）。
    """

    target_id: str
    strength: float = 0.5
    type: str = "related"        # related / causal / temporal / contrast 等


# ---------------------------------------------------------------------------
# 操作日志
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OperationLog:
    """操作日志条目（traceability，debug 用）。

    每次 manager 操作（encode / associate / consolidate / reconsolidate / forget）
    append 一条。retrieve 不记（高频，强化在 strength / access_count 已体现）。
    actor 字段预留 turn_id（Layer 4 Middleware 注入），Layer 2 留 None。

    Args:
        timestamp: 操作发生时间（unix timestamp）。
        operation: 操作名（encode / associate / consolidate / reconsolidate / forget）。
        actor:     操作者标识（thread_id / turn_id，Layer 4 注入；默认 None）。
        diff:      变更摘要（如 {"content": ("old...", "new..."), "strength": (0.5, 0.7)}）。
    """

    timestamp: float                      # 操作发生时间（unix timestamp）
    operation: str                        # encode/associate/consolidate/reconsolidate/forget
    actor: str | None = None              # thread_id / turn_id（Layer 4 注入）
    diff: dict[str, Any] | None = None    # 变更摘要，如 {"content": ("old...", "new..."), "strength": (0.5, 0.7)}


# ---------------------------------------------------------------------------
# 记忆痕迹
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MemoryTrace:
    """记忆痕迹 —— memory 模块的核心数据类。

    frozen 语义：strength 等可变字段通过 with_strength() / with_operation()
    创建新实例替换（类似 skill version DAG 的 is_active 指针）。

    traceability：operation_log 记录操作历史（上限 20 条，超出丢最老），
    支持 debug 回溯「谁何时对这条 trace 做了什么」。

    字段分组：
    - 身份  ：id / content / type。
    - 强度  ：strength / base_strength / decay_rate / access_count /
              last_accessed / importance。
    - 关联  ：associations。
    - 预留  ：embedding（向量库阶段填充）。
    - 溯源  ：source / created_at / metadata。
    - 审计  ：operation_log（上限 20 条 FIFO）。
    """

    id: str
    content: str
    type: MemoryType
    # 强度与衰减
    strength: float = 0.0                # 当前强度 0.0~1.0（lazy decay，retrieve 时计算）
    base_strength: float = 0.7           # 初始强度（由 type 决定，defaults 填充）
    decay_rate: float = 0.1              # 衰减速率（由 type 决定）
    access_count: int = 0                # 累计访问次数
    last_accessed: float = 0.0           # 最后访问时间（unix timestamp）
    importance: float = 0.5              # 语义重要性 0.0~1.0
    # 关联（扩散激活）
    associations: tuple[Association, ...] = field(default_factory=tuple)
    # embedding 预留（默认 None，向量库阶段填充）
    embedding: tuple[float, ...] | None = None
    # Poirot 扩展字段
    source: str | None = None            # 来源（thread_id / run_id / user_input）
    created_at: float = 0.0              # 创建时间
    metadata: dict[str, Any] = field(default_factory=dict)  # tags / project / specialist_id
    # traceability（操作日志，上限 20 条，append-only）
    operation_log: tuple[OperationLog, ...] = field(default_factory=tuple)

    def with_strength(self, new_strength: float, accessed_at: float) -> "MemoryTrace":
        """创建新 trace 替换旧 trace（frozen 语义，检索强化用）。

        检索时自动增强：strength + access_count + last_accessed。
        retrieve 不记 operation_log（高频，强化在 strength / access_count 已体现）。

        Args:
            new_strength: 新的 strength（由 decay_policy 算出）。
            accessed_at:  本次访问时间（unix timestamp）。

        Returns:
            新 MemoryTrace（strength / access_count / last_accessed 更新，其余不变）。

        Raises:
            不主动抛异常。

        组装规则：
            1. 用 replace 派生新实例。
            2. strength 替换为 new_strength。
            3. access_count + 1。
            4. last_accessed 替换为 accessed_at。
        """
        return replace(
            self,
            strength=new_strength,
            access_count=self.access_count + 1,
            last_accessed=accessed_at,
        )

    def with_operation(self, log: OperationLog, *, max_log: int = 20) -> "MemoryTrace":
        """append 一条操作日志（frozen 语义，创建新实例）。

        上限 max_log 条（默认 20），超出丢最老（FIFO）。
        manager 各操作（encode / associate / consolidate / reconsolidate / forget）调用。

        Args:
            log:     待 append 的 OperationLog。
            max_log: operation_log 上限（默认 20）。

        Returns:
            新 MemoryTrace（operation_log 追加一条，超限丢最老）。

        Raises:
            不主动抛异常。

        组装规则：
            1. 旧 operation_log + 新 log（tuple 拼接）。
            2. 长度 > max_log → 保留最近 max_log 条（切片 [-max_log:]）。
            3. 用 replace 派生新实例（只改 operation_log）。
        """
        new_log = self.operation_log + (log,)
        if len(new_log) > max_log:
            new_log = new_log[-max_log:]  # 保留最近 max_log 条
        return replace(self, operation_log=new_log)