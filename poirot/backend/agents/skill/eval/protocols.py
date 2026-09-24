"""Skill eval 评估层 Protocol 抽象 — 三层 eval + 趋势层 + 持久化契约。

【整体职责】
定义 L3 评估层的 5 个 Protocol（接口契约），规定评估层各组件的职责边界：
- 执行层：SkillJudgmentAnalyzer
- 任务层：TaskQualityJudge
- 响应层：ResponseContractChecker
- 趋势层：RuntimeTracker
- 持久化：EvalRunStore

L3 实现 = 实现这些 Protocol + 由 bootstrap 注入，不改动 L2 核心（零侵入）。

【内容摘要】
- SkillJudgmentAnalyzer  : 执行层 eval（LLM 判断 per-skill 是否被应用，
                           同时产出 SkillJudgment + EvolutionSuggestion）。
- TaskQualityJudge       : 任务层 eval（LLM 4 维加权评分 → TaskQualityScore）。
- ResponseContractChecker: 响应层 eval（编译规则检查 candidate → EvalResult）。
- RuntimeTracker         : 趋势层 eval（历史数据 → SkillHealthReport + 退化检测）。
- EvalRunStore           : eval 结果持久化契约（judgment / task_score / eval_run）。

【职责边界】
- 只定义接口（Protocol），不含任何实现。
- 不负责：评估逻辑（在 analyzers / tracker 实现里）、持久化实现（在 store 里）。
- 不 runtime import 实现类：实现由 bootstrap 注入，本模块只声明契约。
- 全部 runtime_checkable：支持 isinstance 检查（便于 bootstrap 装配时校验）。

【INVARIANT】
- 5 个 Protocol 全部 @runtime_checkable。
- SkillJudgmentAnalyzer.analyze_execution 为 async：异步 fire-and-forget，
  不阻塞用户（D-L3-19）。
- SkillJudgmentAnalyzer 同时产出两类：SkillJudgment + EvolutionSuggestion（D-L3-21），
  并在内部调 store.record_outcome 更新 L1 4 计数器（D-L3-3）。
- TaskQualityJudge.judge_task 为 async；4 维权重 0.50 / 0.35 / 0.05 / 0.10（D-L3-13）。
- ResponseContractChecker.check 为同步；ContractCompiler 从 skill 文本自动编译规则，
  外部 skill 零改造（D-L3-4）；结果经 EvalBridge 交给 L2 ScoreDeltaGate。
- RuntimeTracker：health_report 产报告，degraded_skills 供 GitRatchet 按需调用（D-L3-16）。
- EvalRunStore 复用 skills.db v2→v3（D-L3-10）。
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from poirot.backend.agents.skill.evolution.types import EvalResult
from poirot.backend.agents.skill.eval.types import (
    EvolutionSuggestion,
    SkillHealthReport,
    SkillJudgment,
    TaskQualityScore,
)


@runtime_checkable
class SkillJudgmentAnalyzer(Protocol):
    """执行层 eval：每次任务后由 LLM 判断 per-skill 是否被应用。

    职责与约束：
    - D-L3-21：同时产出 EvolutionSuggestion（FIX / DERIVED / CAPTURED + direction）。
    - D-L3-3 ：在内部调 store.record_outcome 更新 L1 的 4 计数器。
    - D-L3-19：异步 fire-and-forget，不阻塞用户。

    方法：
    - analyze_execution(task_id, journal_events, messages_summary, injected_skills)
        返回 (SkillJudgment 列表, EvolutionSuggestion 列表)。
    """

    async def analyze_execution(
        self,
        task_id: str,
        journal_events: list[dict],
        messages_summary: str,
        injected_skills: list[dict],
    ) -> tuple[list[SkillJudgment], list[EvolutionSuggestion]]: ...


@runtime_checkable
class TaskQualityJudge(Protocol):
    """任务层 eval：LLM 4 维加权评分。

    权重（D-L3-13）：0.50*completion + 0.35*quality + 0.05*efficiency + 0.10*tool。

    方法：
    - judge_task(task_id, execution_trace, final_output) → TaskQualityScore。
    """

    async def judge_task(
        self,
        task_id: str,
        execution_trace: str,
        final_output: str,
    ) -> TaskQualityScore: ...


@runtime_checkable
class ResponseContractChecker(Protocol):
    """响应层 eval：从 skill 文本编译规则，检查 candidate 的 SKILL.md。

    职责与约束：
    - D-L3-4：ContractCompiler 从 skill 文本自动编译规则（contract-aware），
      外部 skill 无需改造。
    - 结果走 EvalBridge Protocol 交给 L2 的 ScoreDeltaGate。

    方法：
    - check(candidate_content, baseline_content) → EvalResult（同步）。
    """

    def check(
        self,
        candidate_content: str,
        baseline_content: str,
    ) -> EvalResult: ...


@runtime_checkable
class RuntimeTracker(Protocol):
    """趋势层 eval：从历史数据产出健康报告 + 退化检测。

    职责与约束（D-L3-16）：
    - after_agent 写数据；命令触发时算趋势；
    - GitRatchet 按需调用 degraded_skills。

    方法：
    - health_report(skill_id, window=20) → SkillHealthReport。
    - degraded_skills(threshold=0.15) → list[str]（退化的 skill_id 列表）。
    """

    def health_report(
        self, skill_id: str, window: int = 20,
    ) -> SkillHealthReport: ...

    def degraded_skills(
        self, threshold: float = 0.15,
    ) -> list[str]: ...


@runtime_checkable
class EvalRunStore(Protocol):
    """eval 结果持久化契约。

    复用 skills.db（v2→v3 迁移引入 eval 三表，D-L3-10）。
    实现方通常是 SQLiteSkillStore（它已提供同名方法）。

    方法：
    - save_judgment / save_task_score / save_eval_run  ：写。
    - get_judgments / get_task_scores                  ：读。
    """

    def save_judgment(self, judgment: SkillJudgment) -> str: ...
    def save_task_score(self, score: TaskQualityScore) -> str: ...
    def save_eval_run(self, run: Any) -> str: ...
    def get_judgments(self, skill_id: str, limit: int = 20) -> list[SkillJudgment]: ...
    def get_task_scores(self, task_id: str) -> TaskQualityScore | None: ...