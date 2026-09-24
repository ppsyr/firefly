"""IntentEngineStrengthened — ContextSummarizer 的输入源（非 middleware）。

【整体职责】
分层意图识别：用 IntentTreeStrategy（规则匹配，零 LLM）优先，命中则直接返回；
未命中且 LLM 兜底启用时，调 LLMIntentStrategy（MVP 未实现）。
同时提供 get_candidate_metadata 生成 SpecialistCandidate 列表，供 ContextSummarizer
渲染进 context_summary（per-call 产物，不注入 system prompt）。

【内容摘要】
- IntentAnalysis(frozen)       : 意图识别结果（confidence / candidates / intent_type）。
- IntentStrategy(Protocol)     : 意图识别策略契约。
- IntentTreeStrategy           : 薄包装既有 ReportIntentStrategy.match。
- LLMIntentStrategy            : LLM 兜底（MVP 仅接口，未实现）。
- IntentEngineStrengthened     : 分层意图识别主类（analyze + get_candidate_metadata）。

【职责边界】
- 只负责：意图识别、候选 metadata 生成。
- 不负责：上下文渲染（ContextSummarizer 负责）、specialist 执行、
  specialist 选择（LLM 自决）。
- 不持有运行时状态：tree / llm / metrics_view 由构造或方法参数注入。

【INVARIANT】
- 作为 ContextSummarizer 输入源，不作为 before_model middleware。
- 不注入 system prompt：candidate metadata 通过 context_summary 传递（per-call 产物）。
- 分层策略：IntentTree 优先；confidence >= 0.7 或 llm=None 时直接返回 tree 结果。
- IntentTreeStrategy 不调 detect_and_dispatch：避免 CLI 路由入口的 report action 副作用。
- LLMIntentStrategy MVP 不实现：NotImplementedError → fallback tree 结果。
- LLM 调用异常 → fallback tree 结果（不让意图识别失败阻塞 turn）。
- SpecialistCandidate 不含 capability_match：LLM 自决。
- 无记录的 specialist 用默认值（sample_size=0）——LLM 可据此判断可信度低。
- intent_llm_enabled 默认 false：LLM 兜底数据触发后才启用。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from poirot.backend.agents.intent.engine import ReportIntentStrategy
from poirot.backend.agents.multiagent.evolution.metrics_view import MetricsView
from poirot.backend.agents.multiagent.evolution.types import SpecialistCandidate


@dataclass(frozen=True)
class IntentAnalysis:
    """意图识别结果（IntentTreeStrategy / LLMIntentStrategy 返回）。

    - confidence >= 0.7 视为命中，< 0.7 视为 miss。
    - candidates 为空列表（LLM 兜底启用后才生成 specialist 候选）。
    """

    confidence: float
    candidates: list[str]
    intent_type: str = "unknown"


class IntentStrategy(Protocol):
    """意图识别策略契约（IntentTreeStrategy / LLMIntentStrategy 实现）。"""

    def analyze(
        self,
        user_message: str,
        available_specialists: list[str],
    ) -> IntentAnalysis: ...


class IntentTreeStrategy:
    """薄包装既有 ReportIntentStrategy.match（非 detect_and_dispatch）。

    既有 IntentTree 是单层 root→Report leaf，无 specialist 分类能力。
    IntentTreeStrategy 主要起"confidence 判定 + 零 LLM 成本"作用；
    LLM 兜底才是 specialist 候选生成主力（数据触发后才启用）。

    不调 detect_and_dispatch——CLI 路由入口返 bool 会触发 report action 副作用。
    """

    def __init__(self, confidence_threshold: float = 0.7) -> None:
        """初始化。

        Args:
            confidence_threshold: 命中阈值，默认 0.7。
        """
        self._strategy = ReportIntentStrategy()
        self._threshold = confidence_threshold

    def analyze(
        self,
        user_message: str,
        available_specialists: list[str],
    ) -> IntentAnalysis:
        """调 ReportIntentStrategy.match（纯匹配返 MatchResult），适配为 IntentAnalysis。

        - confidence >= threshold 视为命中，< threshold 视为 miss。
        - candidates 始终空（IntentTree 无 specialist 分类能力）。
        """
        result = self._strategy.match(user_message)
        if result.matched and result.confidence >= self._threshold:
            # payload["type"] 是 IntentType enum，取 .value 转 str
            raw_type = result.payload.get("type")
            intent_type = raw_type.value if hasattr(raw_type, "value") else str(raw_type)
            return IntentAnalysis(
                confidence=result.confidence,
                candidates=[],
                intent_type=intent_type,
            )
        return IntentAnalysis(confidence=0.0, candidates=[], intent_type="unknown")


class LLMIntentStrategy:
    """LLM 兜底（MVP 仅接口签名，不实现）。

    数据触发后才实现（委派率 < 20% 或 ABILITY_INSUFFICIENT > 50%）。
    MVP 阶段 intent_llm_enabled=false，LLMIntentStrategy 不实例化。
    """

    def analyze(
        self,
        user_message: str,
        available_specialists: list[str],
    ) -> IntentAnalysis:
        """MVP 不实现——抛 NotImplementedError。

        数据触发后才实现。
        """
        raise NotImplementedError(
            "LLMIntentStrategy not implemented yet - enable when delegate_rate < 20% "
            "or ABILITY_INSUFFICIENT > 50%"
        )


class IntentEngineStrengthened:
    """分层意图识别（IntentTree 优先 + LLM 兜底）+ candidate metadata 生成。

    作为 ContextSummarizer 输入源，不作为 before_model middleware。
    candidate metadata 通过 ContextSummarizer 渲染进 context_summary（per-call 产物）。

    MVP：intent_llm_enabled=false → llm=None → analyze 始终返回 tree 结果。
    """

    def __init__(
        self,
        tree: IntentTreeStrategy,
        llm: LLMIntentStrategy | None = None,
    ) -> None:
        """初始化。

        Args:
            tree: 规则匹配策略（必传）。
            llm: LLM 兜底策略；None 表示未启用。
        """
        self._tree = tree
        self._llm = llm  # None = LLM 兜底未启用

    def analyze(
        self,
        user_message: str,
        available_specialists: list[str],
    ) -> IntentAnalysis:
        """分层意图识别：IntentTree 优先 + LLM 兜底。

        流程：
        1. 先调 tree.analyze。
        2. confidence >= 0.7 或 llm=None → 直接返回 tree 结果。
        3. 否则调 llm.analyze。
        4. LLM 未实现 / 调用异常 → fallback tree 结果（不阻塞 turn）。
        """
        result = self._tree.analyze(user_message, available_specialists)
        if result.confidence >= 0.7 or self._llm is None:
            return result
        # IntentTree miss + LLM 兜底启用 → 调 LLM
        try:
            return self._llm.analyze(user_message, available_specialists)
        except NotImplementedError:
            # LLM 未实现 → fallback tree（不让意图识别失败阻塞 turn）
            return result
        except Exception:
            # LLM 调用失败 → fallback tree
            return result

    def get_candidate_metadata(
        self,
        goal: str,
        available_specialists: list[str],
        metrics_view: MetricsView,
    ) -> list[SpecialistCandidate]:
        """生成 candidate metadata（不含 capability_match，LLM 自决）。

        供 ContextSummarizer 渲染进 context_summary（per-call 产物）。
        数据来源：metrics_view.get_specialist_metrics 查询历史成功率 / 成本 / 延迟。

        Args:
            goal: 任务目标（MVP 未使用，预留）。
            available_specialists: 可用 specialist 名列表。
            metrics_view: L2 MetricsView Protocol（读 L1 指标）。

        Returns:
            SpecialistCandidate 列表（每个 specialist 一条）。
        """
        candidates: list[SpecialistCandidate] = []
        for name in available_specialists:
            snap = metrics_view.get_specialist_metrics(name)
            if snap is None:
                # 无记录的 specialist 用默认值（sample_size=0，LLM 可判断可信度）
                candidates.append(SpecialistCandidate(
                    name=name,
                    historical_success_rate=0.0,
                    avg_cost_usd=0.0,
                    avg_latency_seconds=0.0,
                    sample_size=0,
                ))
                continue
            candidates.append(SpecialistCandidate(
                name=name,
                historical_success_rate=snap["completion_rate"],
                avg_cost_usd=snap["avg_cost_usd"],
                avg_latency_seconds=snap["avg_latency_seconds"],
                sample_size=snap["sample_size"],
            ))
        return candidates