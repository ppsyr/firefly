"""L3 eval 类型层 — EvalContext + SpecialistHealthReport + DecisionLogRecord。

【整体职责】
定义 L3 评估层的核心数据类型：L2 调 L3 时传入的上下文（EvalContext）、
specialist 健康报告（SpecialistHealthReport）、跨 run 决策日志（DecisionLogRecord）。
同时定义 Trend 字面量类型。全部为 frozen dataclass，作为跨层传递的值对象。

【内容摘要】
- Trend                  : 趋势字面量（improving / stable / degrading / insufficient_data）。
- EvalContext(frozen)    : L2 调 L3 evaluate 时传的上下文。
- SpecialistHealthReport(frozen) : specialist 健康报告（L3 自建，L2 cron 读）。
- DecisionLogRecord(frozen)      : 跨 run 决策日志记录。

【职责边界】
- 只负责：定义数据结构、字段语义、类型。
- 不负责：评估逻辑（adapters 负责）、健康检查计算（runtime_tracker 负责）、
  决策日志存取（db 负责）。
- 不持有状态：全部 frozen dataclass。

【INVARIANT】
- 全部 frozen：不可变，符合 Poirot 值对象原则。
- metadata 用 field(default_factory=dict)：避免可变默认值被共享。
- 复用 L2 类型：EvalTask 来自 evolution/promotion_gate.py，
  EvolutionArtifact / FailureCategory 来自 evolution/types.py——不重复定义。
- 不 import skill 层：L3 自建类型，不复用 skill 的 RuntimeTracker Protocol。
- DecisionLogRecord.success_criteria_met 为 0 / 1 / None：
  None 表示未评估，对应 SQLite INTEGER nullable。
- DecisionLogRecord.failure_category 为 FailureCategory | None：
  None 表示成功调用（无失败分类）。
"""
from dataclasses import dataclass, field
from typing import Any, Literal

from poirot.backend.agents.multiagent.evolution.promotion_gate import EvalTask
from poirot.backend.agents.multiagent.evolution.types import EvolutionArtifact, FailureCategory

# 趋势字面量：改善 / 稳定 / 退化 / 数据不足。
Trend = Literal["improving", "stable", "degrading", "insufficient_data"]


@dataclass(frozen=True)
class EvalContext:
    """L2 调 L3 evaluate 时传入的上下文。

    字段：
    - candidate / baseline: L2 演化产物（EvolutionArtifact Protocol）。
    - task_sample: L2 抽样的历史 specialist 调用记录（EvalTask tuple）。
    - eval_method_hint: L2 建议的评估方法（非强制，Bridge 可覆盖）。
    - profile: specialist profile 名（如 "codex" / "claude"）。
    - metadata: 扩展字段（如 task_type="open_ended" 触发 llm_judge）。
    """

    candidate: EvolutionArtifact
    baseline: EvolutionArtifact
    task_sample: tuple[EvalTask, ...]
    eval_method_hint: str | None = None
    profile: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SpecialistHealthReport:
    """specialist 健康报告。

    L3 自建（不复用 skill 的 RuntimeTracker Protocol——返回类型字段不兼容）。
    SpecialistRuntimeTracker.health_report() 产出，L2 cron 周期读 metrics 算趋势。

    字段：
    - trend: 前后两半比较 completion_rate delta 得出。
    - window_invoked: 窗口内调用次数；< 4 时 trend = insufficient_data。
    - advice: 附加建议文本。
    """

    specialist_name: str
    window_invoked: int
    completion_rate: float
    avg_cost_usd: float
    avg_latency_seconds: float
    fallback_rate: float
    trend: Trend
    advice: str = ""


@dataclass(frozen=True)
class DecisionLogRecord:
    """跨 run 决策日志记录。

    生命周期：
    - L1 tool handler 调 specialist 后异步写（fire-and-forget，不阻塞 L1 turn）。
    - L2 EvolutionMutator 演化时读最近 N 条 lesson 作为输入样本
      （不进 prompt，类似 failure cases）。
    - 保留 90 天；超期归档到 specialist_decision_log_archive 表（不删除）。

    字段：
    - failure_category: L2 FailureCategory enum（4 类）；None 表示成功调用。
    - success_criteria_met: 0 / 1 / None（None 表示未评估，对应 SQLite INTEGER nullable）。
    - lesson_text: 可选的经验文本。
    - timestamp: ISO 格式时间戳；空串表示未填。
    """

    log_id: str
    specialist_name: str
    task_id: str
    goal: str
    success_criteria: str
    failure_category: FailureCategory | None = None
    success_criteria_met: int | None = None
    lesson_text: str | None = None
    timestamp: str = ""