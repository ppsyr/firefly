"""L2 CLI command tree skeleton（保留设计，暂不实现）。

【整体职责】
定义 L2 进化层的 CLI 命令树骨架：8 个 verb（status / history / artifacts /
artifact / metrics / unblock / rollback / intent），只提供接口签名与设计意图，
不做实现——等待更好的可观测形态。

【内容摘要】
- l2_status()     : L2 状态总览（未实现）。
- l2_history()    : 演化历史（未实现）。
- l2_artifacts()  : 演化产物列表（未实现）。
- l2_artifact()   : 单个产物详情（未实现）。
- l2_metrics()    : L2 指标汇总（未实现）。
- l2_unblock()    : 手动释放被阻断 pattern（未实现）。
- l2_rollback()   : 手动回滚到指定版本（未实现）。
- l2_intent()     : IntentEngine 状态（未实现）。
- L2_VERBS        : 8 个 verb 名 tuple（供 CLI dispatcher 用）。

【职责边界】
- 只负责：声明 CLI 命令树的接口签名与设计意图。
- 不负责：实际实现（当前全部 raise NotImplementedError）、CLI 注册（CLI 层负责）、
  数据查询（version_dag / metrics_l2 / intent 负责）。
- 不影响 L2 核心功能：CLI 只是 inspect 工具。

【INVARIANT】
- 全部函数当前 raise NotImplementedError——skeleton 阶段不实现。
- 保留设计的原因：命令行交互复杂、不便观测，等待更好的可观测形态（dashboard / TUI）。
- CLI 是 inspect 工具，不影响 L2 核心功能。
- verb 集合固定为 8 个：status / history / artifacts / artifact / metrics /
  unblock / rollback / intent。
- 带参数的 verb 显式声明（l2_artifact 需 artifact_id，l2_rollback 需 artifact_id + to_version）。
- L2_VERBS 按声明顺序排列，供未来 CLI dispatcher 使用。
"""
from __future__ import annotations

from typing import Any


def l2_status(*args: Any, **kwargs: Any) -> dict:
    """status — L2 状态总览。

    设计输出：enabled / cron interval / cooldown remaining / 当前活跃模板版本 / blocked 列表。
    暂不实现——命令行交互复杂，等待更好的可观测形态（dashboard / TUI）。
    """
    raise NotImplementedError(
        "l2 status not implemented - command-line interaction complex, "
        "wait for better observability form (dashboard / TUI)"
    )


def l2_history(*args: Any, **kwargs: Any) -> list[dict]:
    """history — 演化历史。

    设计输出：最近 N 条实验记录（experiment_id / artifact_type / decision /
    score / timestamp）。
    暂不实现。
    """
    raise NotImplementedError("l2 history not implemented")


def l2_artifacts(*args: Any, **kwargs: Any) -> list[dict]:
    """artifacts — 演化产物列表。

    设计输出：按类型分组，全部版本 + is_active 标记。
    暂不实现。
    """
    raise NotImplementedError("l2 artifacts not implemented")


def l2_artifact(artifact_id: str, *args: Any, **kwargs: Any) -> dict:
    """artifact <id> — 单个产物详情。

    设计输出：完整 dataclass payload + rationale + 实验归属。
    暂不实现。
    """
    raise NotImplementedError("l2 artifact not implemented")


def l2_metrics(*args: Any, **kwargs: Any) -> dict:
    """metrics — L2 指标汇总。

    设计输出：最近 24h 演化次数 / accept 率 / 平均耗时 / 失败分布 / 预算用量。
    暂不实现。
    """
    raise NotImplementedError("l2 metrics not implemented")


def l2_unblock(*args: Any, **kwargs: Any) -> dict:
    """unblock — 手动释放被阻断的 pattern。

    设计选项：--pattern <id> / --eval / --all。
    暂不实现。
    """
    raise NotImplementedError("l2 unblock not implemented")


def l2_rollback(artifact_id: str, to_version: str, *args: Any, **kwargs: Any) -> dict:
    """rollback — 手动回滚到指定版本。

    设计选项：--artifact <id> --to <version>。
    暂不实现。
    """
    raise NotImplementedError("l2 rollback not implemented")


def l2_intent(*args: Any, **kwargs: Any) -> dict:
    """intent — IntentEngine 状态。

    设计子命令：strengthen（启用 LLM fallback）/ reset（禁用）/ status（当前状态）。
    暂不实现。
    """
    raise NotImplementedError("l2 intent not implemented")


# Verb 注册表（供未来 CLI dispatcher 使用）。
L2_VERBS = (
    "status",
    "history",
    "artifacts",
    "artifact",
    "metrics",
    "unblock",
    "rollback",
    "intent",
)