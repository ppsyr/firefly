"""SituationReport — 求助时的结构化态势分析。

【整体职责】
在需要求助（help request）时，生成一份结构化态势报告：程序化部分从 StallTracker
的失败记录中提取尝试、环境与阻塞点；LLM 部分额外调用一次 LLM 综合出 2-4 个可选方案
与推荐项。

【内容摘要】
- SituationReport              : 态势报告数据结构（原因、尝试、环境、阻塞、方案、推荐）。
- SituationReport.to_text      : 渲染为 Markdown 文本。
- build_programmatic_report    : 从 StallTracker 失败记录构建报告的程序化部分。
- _OPTIONS_PROMPT              : 生成方案的 LLM 提示词模板。
- synthesize_options_with_llm  : 调用 LLM 综合方案与推荐（原地修改并返回报告）。

【职责边界】
- 只负责：组织态势报告的数据结构、程序化提取、渲染文本、调用 LLM 补充方案。
- 不负责：失败信号的采集（StallTracker）、停滞判定（StallTracker）、求助的触发与
  处置（stall_detection_middleware / HITL）、LLM 的构造与路由（config / model_router）。

【INVARIANT】
- 两段式构建：程序化部分（build_programmatic_report）+ LLM 部分（synthesize_options_with_llm）。
- 程序化部分必成：attempts / environment / blocker 从失败记录确定性生成。
- LLM 部分容错：调用异常时 options 置空，不向上抛（报告仍可用）。
- 推荐标记：LLM 输出中以 [RECOMMENDED] 开头的行为推荐项，去掉标记后写入 options 并设 recommendation。
- 渲染顺序：Attempts → Environment → Blocker → Options（均为可选段落，无则不渲染）。
- 推荐项渲染标记：to_text 中仅当有 recommendation 时，第 1 项才附 "(recommended)"。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from poirot.backend.agents.observability.stall_tracker import ToolFailure


@dataclass
class SituationReport:
    """态势报告。

    Attributes:
        reason: 求助原因。
        attempts: 尝试记录列表（command / error / capability）。
        environment: 环境信息。
        blocker: 阻塞点描述。
        options: 可选方案列表。
        recommendation: 推荐方案（来自 options 中被标记项）。
    """

    reason: str
    attempts: list[dict[str, Any]] = field(default_factory=list)
    environment: dict[str, Any] = field(default_factory=dict)
    blocker: str = ""
    options: list[str] = field(default_factory=list)
    recommendation: str = ""

    def to_text(self) -> str:
        """渲染为 Markdown 文本。

        Returns:
            str: 含 Attempts / Environment / Blocker / Options 段落的报告文本。
        """
        parts = [f"## Help Request · {self.reason}\n"]
        if self.attempts:
            parts.append("### Attempts")
            for i, a in enumerate(self.attempts, 1):
                parts.append(f"{i}. {a['command']} → {a['error']}")
            parts.append("")
        if self.environment:
            parts.append("### Environment")
            for k, v in self.environment.items():
                parts.append(f"- {k}: {v}")
            parts.append("")
        if self.blocker:
            parts.append(f"### Blocker\n{self.blocker}\n")
        if self.options:
            parts.append("### Options")
            for i, opt in enumerate(self.options, 1):
                marker = " (recommended)" if i == 1 and self.recommendation else ""
                parts.append(f"{i}. {opt}{marker}")
        return "\n".join(parts)


def build_programmatic_report(
    failures: list[ToolFailure],
    reason: str,
    environment: dict[str, Any] | None = None,
) -> SituationReport:
    """Build the programmatic part of a SituationReport from StallTracker data.

    Args:
        failures: StallTracker 的工具失败记录。
        reason: 求助原因。
        environment: 环境信息，可选。

    Returns:
        SituationReport: 含 attempts / environment / blocker 的报告（options 待 LLM 填充）。
    """
    attempts = [
        {"command": f.command, "error": f.error, "capability": f.capability}
        for f in failures
    ]
    caps = {}
    for f in failures:
        caps.setdefault(f.capability, []).append(f.error)
    blocker_parts = []
    for cap, errors in caps.items():
        blocker_parts.append(f"{cap}: {len(errors)} failure(s)")
    blocker = "; ".join(blocker_parts) if blocker_parts else reason
    return SituationReport(
        reason=reason,
        attempts=attempts,
        environment=environment or {},
        blocker=blocker,
    )


_OPTIONS_PROMPT = """Given the following situation, list 2-4 viable options for the user.

Situation: {reason}
Blocker: {blocker}
Attempts:
{attempts}

For each option, provide a short one-line description. Mark the recommended
option with [RECOMMENDED] at the start. Return only the options, one per line."""


def synthesize_options_with_llm(
    report: SituationReport,
    llm: Any,
) -> SituationReport:
    """Call LLM to synthesize options + recommendation. Mutates and returns report.

    Args:
        report: 程序化构建的报告（将被原地补充 options / recommendation）。
        llm: 可调用的 LLM 对象，需支持 invoke。

    Returns:
        SituationReport: 补充方案后的同一报告对象；LLM 异常时 options 置空。
    """
    attempts_text = "\n".join(
        f"- {a['command']} → {a['error']}" for a in report.attempts
    ) or "(no attempts recorded)"
    prompt = _OPTIONS_PROMPT.format(
        reason=report.reason, blocker=report.blocker, attempts=attempts_text,
    )
    try:
        response = llm.invoke(prompt)
        text = response.content if hasattr(response, "content") else str(response)
        lines = [ln.strip() for ln in text.strip().split("\n") if ln.strip()]
        for line in lines:
            if line.startswith("[RECOMMENDED]"):
                report.options.append(line.replace("[RECOMMENDED]", "").strip())
                report.recommendation = report.options[-1]
            else:
                report.options.append(line)
    except Exception:
        report.options = []
    return report