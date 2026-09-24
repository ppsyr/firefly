"""意图识别模块 — agent 核心能力。

【整体职责】
把用户输入识别为结构化意图，并返回"是否已处理"的布尔决策。属 agent core，
与 CLI/API/IM 解耦：调用方（CLI 主循环等）通过 IntentTree.detect_and_dispatch 路由
用户输入——True = 意图已处理（不进 graph），False = 无匹配进 graph。

【内容摘要】
- engine              : 意图识别引擎（三分离架构：节点 + 策略 + 动作）。
- IntentType          : 意图类型枚举（MVP 仅 REPORT）。
- Intent / MatchResult: 意图与匹配结果数据结构。
- IntentStrategy / IntentAction : 匹配策略与动作协议。
- IntentNode / IntentTree       : 意图树节点与遍历器。
- AnyMatchStrategy / ReportIntentStrategy : MVP 策略。
- ReportAction        : MVP 动作（调注入的 handler）。
- default_intent_tree : 构造 MVP 单层树。

【职责边界】
- 只负责：意图匹配、树遍历、动作分发，返回"是否已处理"的布尔决策。
- 不负责：动作的实际执行（handler 由调用方注入）、图的执行（调用方决定）、
  交互呈现（CLI / API / IM）。

【扩展方式】
未来扩展多层意图树 + LLM 识别，只需加 IntentNode + IntentStrategy + IntentAction，
不改 IntentTree 遍历逻辑。

【INVARIANT】
- 返回值语义：True = 已处理（不进 graph），False = 进 graph。
- 三分离：策略只管匹配，动作只管执行，节点只负责组合。
"""
from poirot.backend.agents.intent.engine import (
    AnyMatchStrategy,
    Intent,
    IntentAction,
    IntentNode,
    IntentStrategy,
    IntentTree,
    IntentType,
    MatchResult,
    ReportAction,
    ReportIntentStrategy,
    default_intent_tree,
)

__all__ = [
    "AnyMatchStrategy",
    "Intent",
    "IntentAction",
    "IntentNode",
    "IntentStrategy",
    "IntentTree",
    "IntentType",
    "MatchResult",
    "ReportAction",
    "ReportIntentStrategy",
    "default_intent_tree",
]