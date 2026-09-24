"""Ebbinghaus 衰减策略（lazy decay）。

【整体职责】
实现 DecayPolicy Protocol，提供记忆强度的纯计算。
是 memory 模块唯一的衰减实现，被 forget / manager / retriever 共用。

【核心公式】
    strength = base_strength × (1 - decay_rate)^time_hours
             + log(1 + access_count) × access_boost
             + importance × importance_boost

    第一项：Ebbinghaus 衰减（随时间衰减）
    第二项：访问强化（被反复访问的记忆更强）
    第三项：重要性加成（语义上重要的记忆更强）

【lazy decay】
compute_strength 不修改 trace，只返回 float。
retrieve 时由 Retriever 调用，再用 MemoryTrace.with_strength() 更新。

【职责边界】
- 本模块只算 strength，不做遗忘判定（那是 ForgetPolicy 的事）。
- 不持有状态，无缓存，每次调用都是纯计算。
- 参数从 get_memory_config().decay 取（runtime 可切）；缺省回退 _constants.DECAY_PARAMS。
- 不修改 trace（frozen 语义，返回 float）。

【INVARIANT】
- 纯计算，不持有状态，线程安全。
- 参数 runtime 可切：每次从 get_memory_config().decay 取最新，不缓存。
- 缺省回退 _constants.DECAY_PARAMS。
- 返回值永远钳制在 [0.0, 1.0]。
"""

from __future__ import annotations

import math

from poirot.backend.agents.memory.config import get_memory_config
from poirot.backend.agents.memory.schema import MemoryTrace, MemoryType
from poirot.backend.agents.memory.strategies.default._constants import (
    DECAY_COEFFICIENTS,
    DECAY_PARAMS,
)


class EbbinghausDecayPolicy:
    """Ebbinghaus 衰减策略（DecayPolicy 的默认实现）。

    纯计算，不持有状态，线程安全。
    参数从 get_memory_config().decay 取（runtime 可切），缺省回退 _constants.DECAY_PARAMS。
    """

    def compute_strength(self, trace: MemoryTrace, now: float) -> float:
        """计算当前强度（lazy decay，retrieve 时调用）。

        Args:
            trace: 记忆痕迹（不修改）。
            now:   当前 unix timestamp。

        Returns:
            当前强度，钳制在 0.0~1.0。

        Raises:
            不主动抛异常。

        组装规则：
            1. 按 trace.type 取衰减参数（runtime config 优先，缺省回退 _constants）。
            2. 算 time_hours（自上次访问；未访问过用 created_at）。
            3. 算 Ebbinghaus 衰减项 base_strength × (1 - decay_rate)^time_hours。
            4. 算访问强化项 log(1 + access_count) × access_boost。
            5. 算重要性加成 importance × importance_boost。
            6. 三项相加，钳制到 [0, 1]。
        """
        # 1. 取衰减参数（runtime config 优先，缺省回退 _constants）
        params = self._get_decay_params(trace.type)
        base_strength = params["base_strength"]
        decay_rate = params["decay_rate"]

        # 2. time_hours（自上次访问；未访问用 created_at）
        if trace.last_accessed <= 0:
            time_hours = max(0.0, (now - trace.created_at) / 3600.0)
        else:
            time_hours = max(0.0, (now - trace.last_accessed) / 3600.0)

        # 3. Ebbinghaus 衰减项 base_strength × (1 - decay_rate)^time_hours
        decay_factor = (1.0 - decay_rate) ** time_hours
        decayed_strength = base_strength * decay_factor

        # 4. 访问强化项 log(1 + access_count) × 0.1
        access_boost = math.log(1 + trace.access_count) * DECAY_COEFFICIENTS["access_boost"]

        # 5. 重要性加成 importance × 0.05
        importance_boost = trace.importance * DECAY_COEFFICIENTS["importance_boost"]

        # 6. 合成 + 钳制 [0, 1]
        strength = decayed_strength + access_boost + importance_boost
        return max(0.0, min(1.0, strength))

    def _get_decay_params(self, type: MemoryType) -> dict:
        """取衰减参数（runtime config 优先，缺省回退 _constants）。

        runtime 可切：set_memory_config() 替换 config.decay 后立即生效。

        Args:
            type: 记忆类型（episodic / semantic / procedural）。

        Returns:
            该类型对应的衰减参数 dict（含 base_strength / decay_rate）。

        Raises:
            不主动抛异常；type_key 不在 DECAY_PARAMS 时 KeyError 按原逻辑传播。

        组装规则：
            1. 取 config = get_memory_config()。
            2. 把 type 转成字符串 key（MemoryType → value，其他 → str）。
            3. 若 config.decay 中定义了该 key → 返回 config 的值（runtime 覆盖）。
            4. 否则回退 _constants.DECAY_PARAMS[type_key]（缺省值）。
        """
        config = get_memory_config()
        type_key = type.value if isinstance(type, MemoryType) else str(type)
        # config.decay 覆盖（若该 type 在 config 中有定义）
        if hasattr(config, "decay") and type_key in config.decay:
            return config.decay[type_key]
        # 缺省回退 _constants
        return DECAY_PARAMS[type_key]