"""Skill eval 评估层值对象 — frozen dataclass + Literal 枚举。

【整体职责】
定义 L3 评估层的数据契约（全部为不可变值对象 + Literal 枚举）。
覆盖三层评估 + 两个跨层对象：
- 执行层：SkillJudgment / EvolutionSuggestion
- 任务层：TaskQualityScore
- 响应层：ContractRule
- 趋势层：SkillHealthReport
- 统一审计：EvalRun

这些类型是 eval 层内部产出、store 持久化、evolution 消费的共同语言。
承接 38-skill-eval-layer-design.md §4.1 接口签名。

【内容摘要】
Literal 枚举：
- EvalLayer        : "execution" / "task" / "response"（三层评估标识）。
- Trend            : "improving" / "stable" / "degrading" / "insufficient_data"。
- ContractRuleKind : "programmatic"（确定性检查）/ "llm_binary"（LLM 判断）。

值对象：
- SkillJudgment        : 执行层 per-skill per-task 有效性判断。
- EvolutionSuggestion  : 执行层 LLM 产出的进化建议（喂给 L2 trigger）。
- TaskQualityScore     : 任务层 4 维加权评分。
- ContractRule         : 响应层从 skill 文本编译的契约规则。
- SkillHealthReport    : 趋势层健康报告（RuntimeTracker 产出）。
- EvalRun              : 三层 eval 统一审计记录。

【职责边界】
- 只负责：定义数据契约（字段 + 枚举）。
- 不负责：评估逻辑（analyzers / tracker / bridge）、持久化（store）、决策（evolution）。
- 不持有可变状态：全部 frozen，构造后字段不可改。

【INVARIANT】
- 全部 frozen（不可变值对象）。
- SkillJudgment：per-skill per-task 执行层判断（OpenSpace SkillJudgment 模型）。
- TaskQualityScore：任务层 4 维加权评分，权重 0.50 / 0.35 / 0.05 / 0.10
  （SkillClaw session_judge 模型）。
- ContractRule：响应层从 skill 文本编译的规则（AutoSkill EvalCompiler 模型）。
- SkillHealthReport：趋势层健康报告（RuntimeTracker 产出）。
- EvalRun：三层 eval 统一审计记录。
- EvolutionSuggestion：执行层 LLM 分析产出的进化建议，喂给 L2 trigger。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from poirot.backend.agents.skill.evolution.types import EvolutionType

# ── 枚举（Literal） ──────────────────────────────────────
EvalLayer = Literal["execution", "task", "response"]
Trend = Literal["improving", "stable", "degrading", "insufficient_data"]
ContractRuleKind = Literal["programmatic", "llm_binary"]


@dataclass(frozen=True)
class SkillJudgment:
    """执行层：per-skill per-task 有效性判断（OpenSpace SkillJudgment 模型）。

    与 middleware 粗粒度打点的区别：
    - middleware 只判断 tool 是否命中 allowed_tools；
    - 本类型由 LLM 判断 agent 是否真的**应用了 skill 指导**，更精确。

    字段：
    - judgment_id   : 判断记录 id。
    - skill_id      : 被判断的 skill。
    - skill_name    : 技能名（冗余，便于查询/展示）。
    - task_id       : 所属任务。
    - skill_applied : agent 是否实际应用了 skill 指导（LLM 判断）。
    - deviation_note: 偏差记录（如"跳过了 validate quality 步骤"）。
    - timestamp     : ISO 时间戳。
    """

    judgment_id: str
    skill_id: str
    skill_name: str
    task_id: str
    skill_applied: bool
    deviation_note: str = ""
    timestamp: str = ""


@dataclass(frozen=True)
class EvolutionSuggestion:
    """执行层 LLM 分析产出的进化建议（OpenSpace EvolutionSuggestion 模型）。

    与 SkillJudgment 同源：SkillJudgmentAnalyzer 一次 LLM 调用同时产出二者。
    suggestion 喂给 L2 trigger 作为补充信号（不直接触发进化）。

    字段：
    - evolution_type  : FIX / DERIVED / CAPTURED。
    - target_skill_ids: FIX / DERIVED 有目标；CAPTURED 无（新 skill）。
    - direction       : 自由文本，描述"该修什么 / 该捕获什么"。
    """

    evolution_type: EvolutionType          # FIX / DERIVED / CAPTURED
    target_skill_ids: tuple[str, ...] = ()  # FIX/DERIVED 有；CAPTURED 无（新 skill）
    direction: str = ""                    # 自由文本：该修什么 / 该捕获什么


@dataclass(frozen=True)
class TaskQualityScore:
    """任务层：4 维加权评分（SkillClaw session_judge 模型）。

    权重 D-L3-13：0.50*completion + 0.35*quality + 0.05*efficiency + 0.10*tool。

    字段（各维度取值 [0, 1]）：
    - task_completion  : 任务完成度。
    - response_quality : 响应质量。
    - efficiency       : 效率。
    - tool_usage       : 工具使用。
    - overall_score    : 加权总分。
    - rationale        : 评分理由（LLM 产出）。
    """

    score_id: str
    task_id: str
    task_completion: float          # [0, 1]
    response_quality: float         # [0, 1]
    efficiency: float               # [0, 1]
    tool_usage: float               # [0, 1]
    overall_score: float
    rationale: str = ""
    timestamp: str = ""


@dataclass(frozen=True)
class ContractRule:
    """响应层：从 skill 文本编译的 contract 规则（AutoSkill EvalCompiler 模型）。

    字段：
    - rule_id    : 规则标识，如 "nonempty" / "must_cite" / "json_parseable"。
    - kind       : programmatic（确定性检查）| llm_binary（LLM 判断）。
    - hard       : True → 失败触发 hard_failure（reject 倾向）。
    - description: 规则说明。
    - params     : 规则参数（按 kind 不同而不同）。
    """

    rule_id: str                     # "nonempty" / "must_cite" / "json_parseable" / ...
    kind: ContractRuleKind
    hard: bool
    description: str = ""
    params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SkillHealthReport:
    """趋势层：skill 健康报告（RuntimeTracker 产出）。

    字段：
    - skill_id / skill_name    : 目标 skill。
    - window_selections        : 窗口内 selections 数。
    - applied_rate / completion_rate / effective_rate / fallback_rate : 窗口内 4 rate。
    - trend                    : improving / stable / degrading / insufficient_data。
    - recent_judgments         : 近 N 条 SkillJudgment（偏差记录）。
    - advice                   : 文字建议。
    """

    skill_id: str
    skill_name: str
    window_selections: int
    applied_rate: float
    completion_rate: float
    effective_rate: float
    fallback_rate: float
    trend: Trend
    recent_judgments: tuple[SkillJudgment, ...] = ()
    advice: str = ""


@dataclass(frozen=True)
class EvalRun:
    """一次 eval 运行的审计记录。写 skill_eval_runs 表。

    字段：
    - eval_run_id : 运行 id。
    - eval_layer  : "execution" / "task" / "response"。
    - skill_ids   : 本次评估涉及的 skill。
    - candidate_id: response 层有（候选版本）。
    - baseline_id : response 层有（基线版本）。
    - result_json : 评估结果 JSON。
    - timestamp   : ISO 时间戳。
    """

    eval_run_id: str
    eval_layer: EvalLayer            # "execution" / "task" / "response"
    skill_ids: tuple[str, ...]
    candidate_id: str | None = None  # response 层有
    baseline_id: str | None = None   # response 层有
    result_json: str = ""
    timestamp: str = ""