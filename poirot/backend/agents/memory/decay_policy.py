"""DecayPolicy Protocol — 衰减策略契约。

【整体职责】
定义衰减策略必须提供的接口。
是 memory 模块的 7 个 Protocol 之一，约束 decay 实现（默认 EbbinghausDecayPolicy）。

【核心约定（lazy decay）】
strength 在 retrieve 时按需计算，不跑后台衰减任务。
compute_strength 不修改 trace，只返回 float。

【职责边界】
- 本模块只定义接口签名，零实现（Protocol 纯契约）。
- 只算 strength，不做遗忘判定（那是 ForgetPolicy 的事）。
- 默认实现：EbbinghausDecayPolicy（strategies/default/decay.py，Layer 2）。
- 可替换：LinearDecay / StepDecay / CustomDecay。

【INVARIANT】
- lazy decay：strength 在 retrieve 时按需计算，不跑后台衰减任务。
- compute_strength 不修改 trace（frozen 语义，只返回 float）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from poirot.backend.agents.memory.schema import MemoryTrace


@runtime_checkable
class DecayPolicy(Protocol):
    """衰减策略协议。

    默认实现 EbbinghausDecayPolicy（strategies/default/decay.py，Layer 2）。
    可替换 LinearDecay / StepDecay / CustomDecay。
    """

    def compute_strength(self, trace: MemoryTrace, now: float) -> float:
        """计算当前强度（lazy decay，retrieve 时调用）。

        Ebbinghaus 公式：
            strength = base_strength × (1 - decay_rate)^time_hours
                     + log(1 + access_count) × 0.1
                     + importance × 0.05

        第一项：Ebbinghaus 衰减（随时间衰减）。
        第二项：访问强化（被反复访问的记忆更强）。
        第三项：重要性加成（语义上重要的记忆更强）。

        Args:
            trace: 记忆痕迹（不修改）。
            now:   当前 unix timestamp。

        Returns:
            当前强度 0.0~1.0（钳制）。

        Raises:
            不主动抛异常。

        组装规则：
            1. 按 trace.type 取衰减参数（实现决定来源：config 优先 / 缺省回退）。
            2. 算 time_hours（自上次访问；未访问用 created_at）。
            3. 算 Ebbinghaus 衰减项。
            4. 算访问强化项。
            5. 算重要性加成。
            6. 三项相加，钳制到 [0, 1]。
        """
        ...