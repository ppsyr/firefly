"""默认策略常量（衰减参数 / 检索权重 / 遗忘阈值 / 巩固参数 / 关联默认 / BM25 参数）。

【整体职责】
集中存放默认策略所需的全部常量，供 decay / forget / manager / retriever 使用。
各策略类不内嵌魔法数字，一律从本模块取缺省值；runtime 可切时由 config 覆盖。

【组成】
1. 衰减相关：
   - DECAY_PARAMS         ：三类记忆的衰减参数（base_strength / decay_rate）。
   - DECAY_COEFFICIENTS   ：衰减公式系数（access_boost / importance_boost）。
2. 检索相关：
   - RETRIEVAL_WEIGHTS    ：复合检索分数权重（similarity / strength）。
   - BM25_PARAMS          ：BM25 检索参数（k1 / b / epsilon）。
3. 遗忘相关：
   - FORGET_THRESHOLDS    ：遗忘阈值（strength_threshold / ttl_hours / conflict_window_hours）。
4. 巩固相关：
   - CONSOLIDATE_PARAMS   ：巩固参数（数量范围 / 合并后类型 / importance boost）。
5. 关联相关：
   - ASSOCIATE_DEFAULTS   ：关联默认（强度 / 类型 / 单 trace 上限）。

【分层落地状态】
- Layer 1：仅衰减参数 + 检索权重（供 schema 默认值参考）。
- Layer 2：补全遗忘阈值 / 巩固参数 / 关联默认。
- Layer 3：BM25 参数。

【职责边界】
- 本模块只定义常量，不含任何逻辑，不 import 其他项目内模块。
- 常量值可被 runtime config 覆盖（策略类内部优先读 get_memory_config()，
  缺省回退本模块）。
- 本模块是「缺省值来源」，不是「唯一值来源」；改常量影响缺省行为，
  改 config 影响运行时行为。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 衰减相关
# ---------------------------------------------------------------------------

# 三类记忆的衰减参数（示例值，可调）。
#
# 语义：按记忆类型（episodic / semantic / procedural）给出 base_strength 与 decay_rate。
#   - episodic  ：事件记忆，衰减快（base 低、rate 高）。
#   - semantic  ：语义记忆，衰减慢（提炼后的稳定知识）。
#   - procedural：过程记忆，几乎不衰减（习得技能）。
#
# 用途：EbbinghausDecayPolicy 计算 strength 时按 trace.type 取对应参数；
#       encode 时 A1 决策用 base_strength 作初始强度。
DECAY_PARAMS = {
    "episodic": {"base_strength": 0.7, "decay_rate": 0.1},      # 衰减快
    "semantic": {"base_strength": 0.8, "decay_rate": 0.02},     # 衰减慢
    "procedural": {"base_strength": 0.9, "decay_rate": 0.005},  # 几乎不衰减
}

# 衰减公式系数。
#
# 完整公式（Ebbinghaus）：
#   strength = base_strength × (1 - decay_rate)^time_hours
#            + log(1 + access_count) × access_boost
#            + importance × importance_boost
#
# 用途：access_boost 强化「被反复访问」的记忆；
#       importance_boost 强化「语义上重要」的记忆。
DECAY_COEFFICIENTS = {
    "access_boost": 0.1,        # log(1 + access_count) × 0.1
    "importance_boost": 0.05,   # importance × 0.05
}


# ---------------------------------------------------------------------------
# 检索相关
# ---------------------------------------------------------------------------

# 复合检索分数权重。
#
# 公式：score = similarity × similarity + strength × strength
#              = similarity × 0.7 + strength × 0.3
#
# 用途：RetrievalResult.compute_score 用它合成最终排序分。
#       语义相关性占主导（70%），记忆强度参与排序（30%）。
RETRIEVAL_WEIGHTS = {
    "similarity": 0.7,   # 语义相关性占主导
    "strength": 0.3,     # 记忆强度参与排序
}

# BM25 检索参数。
#
# 用途：HybridRetriever._bm25_score 计算 BM25 分数时使用。
#   - k1     ：TF 饱和参数（词频归一，1.2~2.0 典型）。
#   - b      ：文档长度归一参数（0=不考虑长度，1=完全归一）。
#   - epsilon：IDF 下限平滑（防负 IDF）。
BM25_PARAMS = {
    "k1": 1.5,       # TF 饱和参数（词频归一，1.2~2.0 典型）
    "b": 0.75,       # 文档长度归一参数（0=不考虑长度，1=完全归一）
    "epsilon": 0.25, # IDF 下限平滑（防负 IDF）
}


# ---------------------------------------------------------------------------
# 遗忘相关
# ---------------------------------------------------------------------------

# 遗忘阈值（CompositeForgetPolicy 两规则用）。
#
# 语义：
#   - strength_threshold  ：strength 低于此值 → 遗忘（规则 2）。
#   - ttl_hours           ：超过此小时数未访问 → 遗忘（规则 1，TTL）。
#   - conflict_window_hours：矛盾检测窗口（新记忆 24h 内覆盖旧记忆，预留 Layer 5）。
#
# 用途：CompositeForgetPolicy.should_forget 读它做两规则判定。
FORGET_THRESHOLDS = {
    "strength_threshold": 0.1,   # strength 低于此值 → 遗忘
    "ttl_hours": 720,            # 30 天未访问 → 遗忘（TTL）
    "conflict_window_hours": 24, # 矛盾检测窗口（新记忆 24h 内覆盖旧记忆，预留 Layer 5）
}


# ---------------------------------------------------------------------------
# 巩固相关
# ---------------------------------------------------------------------------

# 巩固参数（Consolidate）。
#
# 语义：
#   - min_traces_to_consolidate   ：最少几条才能合并（少于抛 ValueError）。
#   - max_traces_to_consolidate   ：最多几条一次合并（避免 LLM 上下文爆炸）。
#   - default_consolidated_type   ：合并后新 trace 的默认类型（稳定知识 → semantic）。
#   - default_importance_boost    ：合并后 importance 提升（稳定知识更重要）。
#
# 用途：DefaultMemoryManager.consolidate 做数量校验 + 构造新 trace 时使用。
CONSOLIDATE_PARAMS = {
    "min_traces_to_consolidate": 2,  # 最少 2 条才能合并
    "max_traces_to_consolidate": 10, # 最多 10 条一次合并（避免 LLM 上下文爆炸，E1）
    "default_consolidated_type": "semantic",  # 合并后默认转 semantic
    "default_importance_boost": 0.1, # 合并后 importance 提升（稳定知识更重要）
}


# ---------------------------------------------------------------------------
# 关联相关
# ---------------------------------------------------------------------------

# associate 默认参数。
#
# 语义：
#   - default_strength            ：关联默认强度（未显式传入时用）。
#   - default_type                ：关联默认类型（related / causal / temporal / contrast）。
#   - max_associations_per_trace  ：单 trace 最大关联数（超限 LRU 淘汰最弱，防膨胀）。
#
# 用途：DefaultMemoryManager.associate 构造 Association 时使用。
ASSOCIATE_DEFAULTS = {
    "default_strength": 0.5,     # 默认关联强度
    "default_type": "related",   # related / causal / temporal / contrast
    "max_associations_per_trace": 20,  # 单 trace 最大关联数（防膨胀，D3 LRU 淘汰）
}