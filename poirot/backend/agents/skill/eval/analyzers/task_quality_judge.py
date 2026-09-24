"""TaskQualityJudge — 任务层 eval（SkillClaw session_judge 模型）。

【整体职责】
任务层评估器：任务结束后，用一次 LLM 调用对执行质量做 4 维加权评分，
产出 TaskQualityScore 并持久化。
- 4 维：task_completion / response_quality / efficiency / tool_usage。
- 权重：0.50 / 0.35 / 0.05 / 0.10。
- 触发：post-execution 异步调用。

【内容摘要】
模块常量：
- _WEIGHTS          : 4 维权重字典（0.50 / 0.35 / 0.05 / 0.10）。
- _MAX_TRACE_CHARS  : 执行 trace 字符上限（80000）。
- _MAX_OUTPUT_CHARS : 最终输出字符上限（20000）。

类方法：
- __init__(llm, store)              : 保存依赖（均可为 None）。
- judge_task(...)                   : 对外主入口（async）。
- _llm_judge(...)                   : 调 LLM 4 维评分 + 解析 JSON。
- _extract_json(content)            : 从文本提取首个 JSON 对象。

【职责边界】
- 只负责：任务层 LLM 4 维评分 + 持久化 TaskQualityScore。
- 不负责：执行层判断（SkillJudgmentAnalyzer）、响应层检查（ResponseContractChecker）、
  趋势追踪（RuntimeTracker）、进化建议（EvolutionSuggestion）。
- 不负责调度：异步由 caller 触发。
- 不更新 L1 计数器：只写 task_quality_scores 表，不动 skill_records。

【INVARIANT】
- D-L3-5 / D-L3-13：LLM 4 维加权评分，权重 0.50 / 0.35 / 0.05 / 0.10。
- D-L3-19：post-execution 异步调用。
- graceful degradation：LLM=None 或任何异常 → 返 None，不崩。
- 各维度 clamp 到 [0.0, 1.0]，防 LLM 返回越界值。
- overall_score = round(Σ(dim × weight), 3)。
- 缺省维度值 0.5（LLM 未返回该维度时）。
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from poirot.backend.agents.journal.events import utc_now_iso
from poirot.backend.agents.skill.eval.types import TaskQualityScore

_WEIGHTS = {
    "task_completion": 0.50,
    "response_quality": 0.35,
    "efficiency": 0.05,
    "tool_usage": 0.10,
}

_MAX_TRACE_CHARS = 80000
_MAX_OUTPUT_CHARS = 20000


class TaskQualityJudge:
    """任务层 eval：LLM 4 维加权评分。

    依赖：
    - llm   : 语言模型；None 时 judge_task 直接返 None。
    - store : 持久化；None 时跳过 save_task_score。
    """

    def __init__(self, llm: Any | None = None, store: Any = None) -> None:
        """保存依赖。

        Args:
            llm:   语言模型（可 None）。
            store: 技能存储（可 None，提供 save_task_score）。
        """
        self._llm = llm
        self._store = store

    async def judge_task(
        self,
        task_id: str,
        execution_trace: str,
        final_output: str,
    ) -> TaskQualityScore | None:
        """LLM 4 维评分，返 TaskQualityScore（对外主入口）。

        步骤：
            1. llm is None → 返 None。
            2. 调 _llm_judge；异常 → 返 None。
            3. score 非 None 且 store 非 None → save_task_score（异常 silent）。
            4. 返回 score。

        Args:
            task_id:         任务 id。
            execution_trace: 执行轨迹文本（截断到 _MAX_TRACE_CHARS）。
            final_output:    最终输出（截断到 _MAX_OUTPUT_CHARS）。

        Returns:
            TaskQualityScore；LLM 不可用或异常时返回 None。
        """
        if self._llm is None:
            return None

        try:
            score = self._llm_judge(task_id, execution_trace, final_output)
        except Exception:
            return None

        if score is not None and self._store is not None:
            try:
                self._store.save_task_score(score)
            except Exception:
                pass
        return score

    def _llm_judge(
        self, task_id: str, execution_trace: str, final_output: str,
    ) -> TaskQualityScore | None:
        """调 LLM 4 维评分，解析 JSON 返 TaskQualityScore。

        行为：
        - trace / output 截断到上限。
        - 调 llm.invoke([HumanMessage]).
        - _extract_json 解析；失败 → None。
        - 4 维 clamp 到 [0, 1]；缺省 0.5。
        - overall = Σ(dim × weight)。

        Returns:
            TaskQualityScore；解析失败返回 None。
        """
        from langchain_core.messages import HumanMessage

        trace = (execution_trace or "")[:_MAX_TRACE_CHARS]
        output = (final_output or "")[:_MAX_OUTPUT_CHARS]

        prompt = (
            "你在评估一个 agent 任务的执行质量。\n\n"
            f"执行 trace:\n{trace}\n\n"
            f"最终输出:\n{output}\n\n"
            "4 维评分（0.0-1.0）：\n"
            "- task_completion: 用户目标是否完成\n"
            "- response_quality: 最终输出的正确性/完整性/清晰度\n"
            "- efficiency: 是否避免不必要的重试/绕路\n"
            "- tool_usage: 工具使用是否恰当有效\n\n"
            '只返 JSON: {"task_completion": 0.9, "response_quality": 0.8, '
            '"efficiency": 0.7, "tool_usage": 0.8, "rationale": "简短解释"}'
        )
        resp = self._llm.invoke([HumanMessage(content=prompt)])
        content = resp.content if hasattr(resp, "content") else str(resp)

        data = self._extract_json(content)
        if data is None:
            return None

        dims = {
            k: max(0.0, min(1.0, float(data.get(k, 0.5))))
            for k in _WEIGHTS
        }
        overall = sum(dims[k] * w for k, w in _WEIGHTS.items())

        return TaskQualityScore(
            score_id=f"score_{uuid.uuid4().hex[:12]}",
            task_id=task_id,
            task_completion=dims["task_completion"],
            response_quality=dims["response_quality"],
            efficiency=dims["efficiency"],
            tool_usage=dims["tool_usage"],
            overall_score=round(overall, 3),
            rationale=data.get("rationale", ""),
            timestamp=utc_now_iso(),
        )

    @staticmethod
    def _extract_json(content: str) -> dict | None:
        """从文本提取首个 JSON 对象（'{' 到末个 '}'）。

        找不到或解析失败 → None。
        """
        s = content.find("{")
        e = content.rfind("}")
        if s != -1 and e != -1 and e > s:
            try:
                return json.loads(content[s : e + 1])
            except Exception:
                return None
        return None