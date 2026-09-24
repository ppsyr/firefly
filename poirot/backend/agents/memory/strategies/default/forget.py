"""Composite 遗忘策略（TTL + strength，两规则）。

【整体职责】
实现 ForgetPolicy Protocol，提供记忆的自动遗忘判定。
是 memory 模块唯一的遗忘实现，被 manager 在 Phase 2 / 遗忘触发时调用。

【两规则组合】
should_forget 检查两条规则，命中任一即应遗忘：
1. TTL 过期：now - last_access > ttl_hours × 3600
2. strength 阈值：compute_strength(trace, now) < strength_threshold

【职责边界】
- 本模块只判「是否该忘」，不做实际删除（那是 store 的事）。
- 不含矛盾解决（resolve_conflict 已删）。矛盾解决走 reconsolidate（单条内容更新）
  或 consolidate（多条合并 + 旧标记 forgotten），由 manager 调用。
- 规则 2 依赖 DecayPolicy 计算当前 strength（构造时注入，缺省用 EbbinghausDecayPolicy）。
- 参数从 get_memory_config().forget 取（runtime 可切）；缺省回退 _constants.FORGET_THRESHOLDS。

【INVARIANT】
- 两规则遗忘：should_forget 检查 TTL + strength；无 resolve_conflict。
- 依赖 DecayPolicy 计算当前 strength（规则 2）。
- 参数 runtime 可切：每次从 get_memory_config().forget 取最新，不缓存。
- 缺省回退 _constants.FORGET_THRESHOLDS。
"""

from __future__ import annotations

from poirot.backend.agents.memory.config import get_memory_config
from poirot.backend.agents.memory.schema import MemoryTrace
from poirot.backend.agents.memory.strategies.default._constants import FORGET_THRESHOLDS
from poirot.backend.agents.memory.strategies.default.decay import EbbinghausDecayPolicy


class CompositeForgetPolicy:
    """Composite 遗忘策略（ForgetPolicy 的默认实现）。

    两规则组合（should_forget 检查）：
    1. TTL 过期：now - last_accessed > ttl_hours × 3600
    2. strength 阈值：compute_strength(trace, now) < strength_threshold

    不含矛盾解决（resolve_conflict 已删）。矛盾解决走 reconsolidate
    （单条内容更新）或 consolidate（多条合并 + 旧标记 forgotten），由 manager 调用。

    依赖 DecayPolicy 计算当前 strength（规则 2）。
    参数从 get_memory_config().forget 取（runtime 可切），缺省回退 _constants.FORGET_THRESHOLDS。
    """

    def __init__(self, decay_policy: EbbinghausDecayPolicy | None = None) -> None:
        """初始化。

        Args:
            decay_policy: 衰减策略，用于规则 2 strength 计算。
                          None 时内部构造默认 EbbinghausDecayPolicy。

        Returns:
            None。

        Raises:
            不主动抛异常。

        组装规则：
            1. 若传入 decay_policy 非 None → 使用它。
            2. 否则构造 EbbinghausDecayPolicy()（缺省衰减策略）。
            3. 存到 self._decay_policy。
        """
        self._decay_policy = decay_policy or EbbinghausDecayPolicy()

    def should_forget(self, trace: MemoryTrace, now: float) -> bool:
        """是否应遗忘该记忆（自动规则 1+2）。

        Args:
            trace: 记忆痕迹（不修改）。
            now:   当前 unix timestamp。

        Returns:
            True 表示应遗忘（TTL 过期 或 strength 低于阈值）；否则 False。

        Raises:
            不主动抛异常。

        组装规则：
            1. 取遗忘阈值（runtime config 优先，缺省回退 _constants）。
            2. 规则 1：算 last_access（last_accessed > 0 用它，否则用 created_at），
               若 now - last_access > ttl_hours × 3600 → 返回 True。
            3. 规则 2：调 decay_policy.compute_strength 算当前强度，
               若 < strength_threshold → 返回 True。
            4. 两规则都未命中 → 返回 False。
        """
        thresholds = self._get_thresholds()

        # 规则 1：TTL 过期（长期未访问；last_accessed<=0 用 created_at）
        last_access = trace.last_accessed if trace.last_accessed > 0 else trace.created_at
        ttl_seconds = thresholds["ttl_hours"] * 3600.0
        if (now - last_access) > ttl_seconds:
            return True

        # 规则 2：strength 低于阈值（lazy decay 计算）
        current_strength = self._decay_policy.compute_strength(trace, now)
        if current_strength < thresholds["strength_threshold"]:
            return True

        return False

    def _get_thresholds(self) -> dict:
        """取遗忘阈值（runtime config 优先，缺省回退 _constants）。

        runtime 可切：set_memory_config() 替换 config.forget 后立即生效。

        Args:
            无。

        Returns:
            阈值 dict，含 strength_threshold / ttl_hours / conflict_window_hours。

        Raises:
            不主动抛异常。

        组装规则：
            1. 取 config = get_memory_config()。
            2. 若 config.forget 存在且非空：
               - 逐项从 config.forget 取（缺失时回退 FORGET_THRESHOLDS 对应值）。
               - 返回合并后的 dict。
            3. 否则直接返回 _constants.FORGET_THRESHOLDS（缺省值）。
        """
        config = get_memory_config()
        if hasattr(config, "forget") and config.forget:
            return {
                "strength_threshold": config.forget.get(
                    "strength_threshold", FORGET_THRESHOLDS["strength_threshold"]
                ),
                "ttl_hours": config.forget.get(
                    "ttl_hours", FORGET_THRESHOLDS["ttl_hours"]
                ),
                "conflict_window_hours": config.forget.get(
                    "conflict_window_hours", FORGET_THRESHOLDS["conflict_window_hours"]
                ),
            }
        return FORGET_THRESHOLDS