"""PersonaPolicy Protocol — 用户画像策略契约。

【整体职责】
定义用户画像策略必须提供的接口。
是 memory 模块的 7 个 Protocol 之一，约束 persona 实现（默认 StaticDynamicPersona）。
当前属 Layer 6 预留：接口签名完整，暂无实现、无主链引用。

【画像二分（借鉴 supermemory）】
- static profile ：稳定事实（偏好 / 长期目标）。
- dynamic profile：近期活动（当前任务 / 最近对话）。

【职责边界】
- 本模块只定义接口签名，零实现（Protocol 纯契约）。
- 画像归纳来源：从 semantic 记忆归纳（update_profile 由上层调用）。
- 默认实现：StaticDynamicPersona（strategies/default/persona.py，Layer 6）。
- 可替换：FlatPersona / NoPersona。

【INVARIANT】
- Protocol 纯契约零实现，Layer 1 仅定义接口，Layer 6 填实现。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class PersonaPolicy(Protocol):
    """用户画像策略协议（supermemory profile.static + dynamic 二分）。

    默认实现 StaticDynamicPersona（strategies/default/persona.py，Layer 6）。
    可替换 FlatPersona / NoPersona。
    """

    def get_static_profile(self, user_id: str) -> dict:
        """取稳定事实（偏好 / 长期目标）。

        Args:
            user_id: 用户标识。

        Returns:
            稳定事实 dict。

        Raises:
            不主动抛异常。

        组装规则：
            1. 按 user_id 取 static profile 并返回（实现决定数据来源）。
        """
        ...

    def get_dynamic_profile(self, user_id: str) -> dict:
        """取近期活动（当前任务 / 最近对话）。

        Args:
            user_id: 用户标识。

        Returns:
            近期活动 dict。

        Raises:
            不主动抛异常。

        组装规则：
            1. 按 user_id 取 dynamic profile 并返回（实现决定数据来源）。
        """
        ...

    def update_profile(self, user_id: str, facts: dict) -> None:
        """更新画像（从 semantic 记忆归纳）。

        Args:
            user_id: 用户标识。
            facts:   待更新的画像事实 dict。

        Returns:
            None。

        Raises:
            不主动抛异常。

        组装规则：
            1. 把 facts 合并 / 写入 user_id 的画像（实现决定合并策略）。
        """
        ...