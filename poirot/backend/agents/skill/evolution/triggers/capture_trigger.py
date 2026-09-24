"""CaptureTrigger — CAPTURED 主动沉淀触发（补 Hermes 式）。

【整体职责】
CAPTURED 类进化的触发器：把"可复用的成功模式"沉淀为新 skill。
2a 仅支持手动 `/skill capture`（manual_capture）；
自动信号（重复成功模式 / agent 自评）留 2b。

【内容摘要】
- should_trigger(store)                     : 2a 自动信号未实现，返空列表。
- manual_capture(pattern, suggested_name)   : 手动沉淀入口，产 CAPTURED 上下文。

【职责边界】
- 只负责：产 EvolutionContext（CAPTURED 类型）——不聚焦、不变异、不评估、不门控。
- 不负责：聚焦（focuser）、变异（mutator）、评估（eval_bridge）、门控（gate）、
  持久化（store）、编排（EvolutionManager）。
- 2a 不做自动识别：should_trigger 恒返空；自动模式识别留 2b。

【INVARIANT】
- 2a 仅手动 capture（manual_capture）；should_trigger 返空。
- manual_capture 产的 EvolutionContext：
    trigger="CAPTURE"、evolution_type="CAPTURED"、
    target_skill=None（新 skill，无基线）、
    capture_pattern=pattern、suggested_name=suggested_name。
- 自动信号（重复成功模式 / agent 自评）留 2b（post-execution 模式识别 + agent 自评）。
"""
from __future__ import annotations

from typing import Any

from poirot.backend.agents.skill.evolution.types import EvolutionContext


class CaptureTrigger:
    """CAPTURED 触发。

    2a：仅手动 capture；should_trigger 返空。
    2b：应补 post-execution 模式识别 + agent 自评（自动信号）。
    """

    def should_trigger(self, store: Any) -> list[EvolutionContext]:
        """扫自动信号，返回待 CAPTURED 的上下文列表。

        2a 自动信号未实现，恒返空。
        2b 计划：post-execution 模式识别 + agent 自评。

        Args:
            store: 技能存储（当前未使用；为满足 Trigger Protocol 保留）。

        Returns:
            空列表（2a）。
        """
        return []

    def manual_capture(self, pattern: str, suggested_name: str) -> EvolutionContext:
        """手动沉淀入口（/skill capture 命令调）。

        产 CAPTURED 上下文：
        - trigger="CAPTURE"
        - evolution_type="CAPTURED"
        - target_skill=None（新 skill，无基线）
        - capture_pattern=pattern（可复用模式描述）
        - suggested_name=suggested_name（建议 skill name）

        Args:
            pattern:        可复用模式描述。
            suggested_name: 建议 skill name。

        Returns:
            EvolutionContext（CAPTURED）。
        """
        return EvolutionContext(
            trigger="CAPTURE",
            evolution_type="CAPTURED",
            target_skill=None,
            capture_pattern=pattern,
            suggested_name=suggested_name,
        )