"""意图识别引擎 — agent 核心能力（理解用户需求）。

【整体职责】
把用户输入识别为结构化意图，并决定是否"提前处理"（不进 graph）还是放行进 graph。
采用三分离架构：IntentNode（节点）+ IntentStrategy（匹配策略）+ IntentAction（动作），
MVP 为单层树（root → ReportIntent leaf）。与 CLI 解耦，CLI / API / IM 均可作为消费者
调用 IntentTree.detect_and_dispatch；路由决策返回 bool，由调用方决定是否进 graph。

【内容摘要】
- IntentType        : 意图类型枚举（MVP 仅 REPORT）。
- Intent            : 匹配成功的意图（type / confidence / payload）。
- MatchResult       : 策略匹配结果（matched / confidence / payload / children）。
- IntentStrategy    : 匹配策略协议（MVP 正则，未来可换 LLM）。
- IntentAction      : 匹配后动作协议（返回 bool 决定是否进 graph）。
- IntentNode        : 意图树节点（叶子有 action，中间节点有 children）。
- IntentTree        : 意图树遍历器（detect_and_dispatch）。
- AnyMatchStrategy  : root 策略，匹配任意输入进入 children。
- ReportIntentStrategy : 报告意图策略，保守关键词整句匹配（^ 锚定）。
- ReportAction      : 报告动作，调注入的 handler 触发报告合成。
- _extract_topic    : 从输入中提取 topic。
- default_intent_tree : 构造 MVP 单层树。

【职责边界】
- 只负责：意图匹配、树遍历、动作分发，返回"是否已处理"的布尔决策。
- 不负责：报告合成的实际执行（handler 由调用方注入）、图的执行（调用方决定）、
  交互呈现（CLI / API / IM）、多意图类型的完整覆盖（MVP 仅 REPORT）。

【INVARIANT】
- 三分离：策略只管匹配，动作只管执行，节点只负责组合——互不耦合。
- 返回值语义：detect_and_dispatch / execute 返回 True = 已处理（不进 graph），False = 进 graph。
- 匹配顺序：先 root 策略，matched 后若有 action 则执行，否则遍历 children。
- 策略可替换：IntentStrategy 为 Protocol，未来实现 LLMIntentStrategy 时接口不变。
- 动作可注入：ReportAction 的 handler 由调用方提供，None 时 execute 返回 False（不处理）。
- 保守匹配：ReportIntentStrategy 用 ^ 锚定整句，避免"如何写报告"这类被误触发。
- 树可扩展：未来加多层 + LLM 识别只需加 Node + Strategy + Action。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Protocol


class IntentType(Enum):
    """意图类型枚举。

    Attributes:
        REPORT: 报告意图（MVP 仅此一种）。
    """

    REPORT = "report"
    # 未来: DEEP_RESEARCH, CLARIFY_TOPIC, CLARIFY_SCOPE ...


@dataclass
class Intent:
    """匹配成功的意图。

    Attributes:
        type: 意图类型。
        confidence: 置信度。
        payload: 意图负载（如 topic）。
    """

    type: IntentType
    confidence: float
    payload: dict


@dataclass
class MatchResult:
    """策略匹配结果。children 非空表示进子节点（树扩展）。

    Attributes:
        matched: 是否匹配。
        confidence: 置信度。
        payload: 匹配负载。
        children: 子节点列表，用于树扩展；None 表示不进子节点。
    """

    matched: bool
    confidence: float
    payload: dict
    children: list[IntentNode] | None = None


class IntentStrategy(Protocol):
    """匹配策略。MVP 正则，未来可换 LLM（实现 LLMIntentStrategy 替换，接口不变）。"""

    def match(self, text: str) -> MatchResult: ...


class IntentAction(Protocol):
    """匹配后执行的动作。解耦：策略不管动作，动作不管匹配逻辑。

    返回 True = 已处理（不进 graph），False = 未处理（继续进 graph）。
    """

    def execute(self, intent: Intent, runtime: Any) -> bool: ...


@dataclass
class IntentNode:
    """意图树节点。叶子有 action，中间节点有 children。

    Attributes:
        strategy: 该节点的匹配策略。
        action: 叶子动作；非叶子为 None。
        children: 子节点列表；叶子为 None。
    """

    strategy: IntentStrategy
    action: IntentAction | None = None
    children: list[IntentNode] | None = None


class IntentTree:
    """意图树遍历器。

    detect_and_dispatch: root 匹配 → 进 children 或执行 action。
    返回 True = 已处理，False = 无匹配进 graph。

    Attributes:
        _root: 根节点。
    """

    def __init__(self, root: IntentNode) -> None:
        """初始化。

        Args:
            root: 意图树根节点。
        """
        self._root = root

    def detect_and_dispatch(self, text: str, runtime: Any) -> bool:
        """从根开始匹配并分发。

        Args:
            text: 用户输入。
            runtime: 运行期对象（透传给动作）。

        Returns:
            bool: True = 已处理（不进 graph），False = 进 graph。
        """
        return self._traverse(self._root, text, runtime)

    def _traverse(self, node: IntentNode, text: str, runtime: Any) -> bool:
        """递归遍历节点：匹配 → 执行动作或进入子节点。

        Args:
            node: 当前节点。
            text: 用户输入。
            runtime: 运行期对象。

        Returns:
            bool: True = 已处理，False = 未处理。
        """
        result = node.strategy.match(text)
        if not result.matched:
            return False
        if node.action is not None:
            intent = Intent(
                type=result.payload.get("type", IntentType.REPORT),
                confidence=result.confidence,
                payload=result.payload,
            )
            return bool(node.action.execute(intent, runtime))
        children = result.children or node.children
        if children:
            for child in children:
                if self._traverse(child, text, runtime):
                    return True
        return False


# --------------------------------------------------------------------------- #
# MVP 策略 + 动作
# --------------------------------------------------------------------------- #


class AnyMatchStrategy:
    """root 节点策略：匹配任意输入（进入 children）。"""

    def match(self, text: str) -> MatchResult:
        return MatchResult(matched=True, confidence=1.0, payload={}, children=None)


class ReportIntentStrategy:
    """报告意图策略：保守关键词整句匹配（^锚定），防误触发。

    匹配模式：生成报告 / 出报告 / 整理成报告 / 现在开始生成报告 /
    写一份报告 / 给我一份报告 / /report。
    不匹配："如何写报告"（不以关键词开头）。

    Attributes:
        PATTERNS: 匹配模式列表（均以 ^ 锚定）。
    """

    PATTERNS = [
        r"^生成报告",
        r"^出报告",
        r"^整理成报告",
        r"^现在开始生成报告",
        r"^写一份报告",
        r"^给我一份报告",
        r"^/report\b",
    ]

    def match(self, text: str) -> MatchResult:
        """匹配报告意图；命中则提取 topic。

        Args:
            text: 用户输入。

        Returns:
            MatchResult: 命中时 payload 含 type 与 topic。
        """
        stripped = text.strip()
        for pattern in self.PATTERNS:
            if re.match(pattern, stripped, re.IGNORECASE):
                topic = _extract_topic(stripped)
                return MatchResult(
                    matched=True,
                    confidence=1.0,
                    payload={"type": IntentType.REPORT, "topic": topic},
                    children=None,
                )
        return MatchResult(matched=False, confidence=0.0, payload={}, children=None)


class ReportAction:
    """报告动作：调 handler 触发报告合成。

    handler 由调用方注入（CLI/API/IM），保持 intent 模块与报告合成流程解耦。
    handler 签名：(intent: Intent, runtime: Any) -> bool。

    Attributes:
        _handler: 注入的处理函数；None 时 execute 返回 False。
    """

    def __init__(self, handler: Callable[[Intent, Any], bool] | None = None) -> None:
        """初始化。

        Args:
            handler: 报告处理函数，可选。
        """
        self._handler = handler

    def execute(self, intent: Intent, runtime: Any) -> bool:
        """执行报告动作。

        Args:
            intent: 匹配到的意图。
            runtime: 运行期对象。

        Returns:
            bool: handler 存在时返回其结果，否则 False。
        """
        if self._handler is None:
            return False
        return bool(self._handler(intent, runtime))


def _extract_topic(text: str) -> str | None:
    """从 "/report 天气" 或 "生成报告 天气" 提取 topic。无则 None。

    Args:
        text: 用户输入。

    Returns:
        str | None: 提取到的 topic；无则 None。
    """
    m = re.match(r"^/report\s+(.+)$", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.match(r"^(?:生成|出|整理成|现在开始生成|写一份|给我一份)报告\s+(.+)$", text)
    if m:
        return m.group(1).strip()
    return None


def default_intent_tree(report_handler: Callable[[Intent, Any], bool] | None = None) -> IntentTree:
    """MVP 单层树：root → ReportIntent leaf。

    report_handler 由调用方注入；None 时 ReportAction.execute 返回 False（不处理）。

    Args:
        report_handler: 报告处理函数，可选。

    Returns:
        IntentTree: 单层意图树。
    """
    root = IntentNode(
        strategy=AnyMatchStrategy(),
        children=[
            IntentNode(
                strategy=ReportIntentStrategy(),
                action=ReportAction(handler=report_handler),
            ),
        ],
    )
    return IntentTree(root)