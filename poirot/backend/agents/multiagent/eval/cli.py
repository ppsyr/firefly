"""L3 CLI 命令树设计 skeleton（保留设计，暂不实现）。

【整体职责】
定义 L3 评估层的 CLI 命令树骨架：5 个 verb（status / health / decision-log /
eval-history / degraded），只提供接口签名与设计意图，不做实现——等待更好的可观测形态。

【内容摘要】
- L3_VERBS                : 5 个 verb 名 tuple。
- l3_status()             : L3 状态总览（未实现）。
- l3_health()             : specialist 健康报告（未实现）。
- l3_decision_log()       : 决策日志查询（未实现）。
- l3_eval_history()       : 评估历史查询（未实现）。
- l3_degraded()           : degraded specialist 查询（未实现）。

【职责边界】
- 只负责：声明 CLI 命令树的接口签名与设计意图。
- 不负责：实际实现（当前全部 raise NotImplementedError）、CLI 注册（CLI 层负责）、
  数据查询（runtime_tracker / decision_log 负责）。
- 不持有状态：所有函数只是占位。

【INVARIANT】
- 全部函数当前 raise NotImplementedError——skeleton 阶段不实现。
- 保留设计的原因：命令方式交互复杂、不便观测，等待更好的可观测形态。
- 未来实现触发条件：用户主动需求 / 更好可观测形态出现 / L3 实际落地后需要 inspect 工具。
- verb 集合固定为 5 个：status / health / decision-log / eval-history / degraded。
- 所有函数签名统一为 (*args, **kwargs) -> dict——当前不约定具体参数。
"""
from __future__ import annotations

from typing import Any

# L3 CLI 的 5 个 verb 名。
L3_VERBS = ("status", "health", "decision-log", "eval-history", "degraded")


def l3_status(*args: Any, **kwargs: Any) -> dict:
    """L3 状态总览。

    设计意图：展示 enabled / 已注册 adapter / 当前 default method / 健康监控窗口。
    暂不实现——等待更好的可观测形态。
    """
    raise NotImplementedError("L3 CLI status not implemented (L3-9.2: await better observability)")


def l3_health(*args: Any, **kwargs: Any) -> dict:
    """specialist 健康报告。

    设计意图：返回 per-specialist SpecialistHealthReport 列表。
    暂不实现——等待更好的可观测形态。
    """
    raise NotImplementedError("L3 CLI health not implemented (L3-9.2: await better observability)")


def l3_decision_log(*args: Any, **kwargs: Any) -> dict:
    """决策日志查询。

    设计意图：按 specialist / failure_category / 时间过滤。
    暂不实现——等待更好的可观测形态。
    """
    raise NotImplementedError("L3 CLI decision-log not implemented (L3-9.2: await better observability)")


def l3_eval_history(*args: Any, **kwargs: Any) -> dict:
    """评估历史查询。

    设计意图：返回最近 N 次 EvalRun。
    暂不实现——等待更好的可观测形态。
    """
    raise NotImplementedError("L3 CLI eval-history not implemented (L3-9.2: await better observability)")


def l3_degraded(*args: Any, **kwargs: Any) -> dict:
    """degraded specialist 查询。

    设计意图：返回 degraded_specialists() 的输出。
    暂不实现——等待更好的可观测形态。
    """
    raise NotImplementedError("L3 CLI degraded not implemented (L3-9.2: await better observability)")