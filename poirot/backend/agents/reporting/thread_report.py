"""渠道无关的报告生成服务 — 从 thread 累积 state 合成报告 + 保存 artifact。

【整体职责】
为 CLI / API / IM 等所有交互渠道提供统一的报告生成入口：
从 checkpointer 取 thread 累积 state，调用 reporter 合成报告，按配置保存 artifact。
各渠道自行负责 presentation（console / HTTP / IM），本模块只产结果、不碰呈现。

【内容摘要】
- ReportArtifact        : 报告生成结果（渠道无关），含报告文本与 artifact 路径。
- _ReportRuntime        : Protocol，描述本模块所需 runtime 的形状（AppRuntime 结构性满足）。
- generate_report_from_thread : 主入口，取 state → 合成 → 可选保存 artifact。

【职责边界】
- 只负责：取 thread state、合成报告、按配置保存 artifact，返回渠道无关的 ReportArtifact。
- 不负责：交互呈现（console / HTTP / IM）、报告文本渲染逻辑（reporter）、
  artifact 存储实现（artifact_store）、runtime 的构造与生命周期。

【依赖方向】
agents 层不 import app 层，故用 Protocol（_ReportRuntime）描述 runtime 形状，
AppRuntime 结构性满足，避免反向依赖。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from poirot.backend.agents.reporting.report_store import ReportStore, SavedReport


@dataclass
class ReportArtifact:
    """报告生成结果。渠道无关。

    Attributes:
        final_report: 合成的最终报告文本。
        artifact_path: 保存的 artifact 路径；未保存时为 None。
        report_path: 手动用户级报告路径；自动 export 时为 None。
        conversation_path: 手动报告对应的 JSONL 对话副本路径。
    """

    final_report: str
    artifact_path: str | None
    report_path: str | None = None
    conversation_path: str | None = None
    title: str | None = None
    thread_id: str | None = None


class _ReportRuntime(Protocol):
    """generate_report_from_thread 需要的 runtime 形状。AppRuntime 结构性满足。

    Attributes:
        leader_agent: Leader Agent，需含 .graph（提供 get_state）。
        thread_id: 当前线程 ID，用于取 checkpointer 累积 state。
        capability_registry: 能力注册表，需含 get_reporter / get_artifact_store。
        thread_dir: 线程产物目录，作为 artifact 输出目录。
        config: 运行配置，需含 .reporting.save_artifact。
    """

    leader_agent: Any  # 含 .graph
    thread_id: str
    capability_registry: Any  # 含 get_reporter / get_artifact_store
    thread_dir: Path
    config: Any  # 含 .reporting.save_artifact


def generate_report_from_thread(
    runtime: _ReportRuntime,
    topic: str | None = None,
) -> ReportArtifact:
    """从 thread 累积 state 合成报告 + 保存 artifact。

    Args:
        runtime: 满足 _ReportRuntime 形状的运行时对象（如 AppRuntime）。
        topic: 可选主题；非空时覆盖 state 中的 research_question。

    Returns:
        ReportArtifact: 报告文本与（可选的）artifact 路径。

    流程：
        1. graph.get_state({"configurable": {"thread_id": runtime.thread_id}})
           取 checkpointer 累积 state。
        2. topic 非空 → 覆盖 state["research_question"]。
        3. reporter.generate_report(state) 合成报告
           （三级 fallback：final_report → observations/sources → last AIMessage）。
        4. 若 config.reporting.save_artifact 为真 → artifact_store.save_artifact(...) 保存。
    """
    config = {"configurable": {"thread_id": runtime.thread_id}}
    snapshot = runtime.leader_agent.graph.get_state(config)
    state: dict[str, Any] = (
        dict(snapshot.values) if snapshot and snapshot.values else {}
    )
    if topic:
        state["research_question"] = topic

    reporter = runtime.capability_registry.get_reporter()
    result = reporter.generate_report(state, run_context=None)

    artifact_path: str | None = None
    if runtime.config.reporting.save_artifact:
        artifact = runtime.capability_registry.get_artifact_store().save_artifact(
            content=result.final_report,
            output_dir=runtime.thread_dir,
            title=topic or "Report",
            filename="report.md",
            metadata={"mode": "default", "topic": topic or ""},
        )
        artifact_path = artifact.path
    return ReportArtifact(final_report=result.final_report, artifact_path=artifact_path)


def _thread_state(runtime: _ReportRuntime) -> dict[str, Any]:
    """Read the latest committed state without changing the runtime."""
    config = {"configurable": {"thread_id": runtime.thread_id}}
    snapshot = runtime.leader_agent.graph.get_state(config)
    if not snapshot or not getattr(snapshot, "values", None):
        return {}
    return dict(snapshot.values)


def persist_report_from_thread(
    runtime: _ReportRuntime,
    title: str | None = None,
) -> ReportArtifact:
    """Generate and persist a manual report under the user storage root.

    This path is intentionally separate from ``generate_report_from_thread``:
    automatic export keeps its run artifact contract, while ``/report`` creates
    a paired, durable Markdown and conversation snapshot.
    """
    state = _thread_state(runtime)
    messages = state.get("messages") or []
    has_material = bool(
        messages
        or state.get("final_report")
        or state.get("observations")
        or state.get("sources")
        or state.get("errors")
    )
    if not has_material:
        raise ValueError("当前 thread 没有可生成报告的已保存历史")

    reporter = runtime.capability_registry.get_reporter()
    result = reporter.generate_report(state, run_context=None)
    thread_store = getattr(runtime, "thread_store", None)
    item = thread_store.require(runtime.thread_id) if thread_store is not None else None
    report_title = title if title is not None else (getattr(item, "title", None) or "Report")
    if title is not None and not title.strip():
        raise ValueError("报告标题不能为空")
    thread_location = ""
    storage_root = getattr(getattr(runtime, "config", None), "runtime", None)
    storage_value = getattr(storage_root, "storage_root", None)
    if storage_value is None:
        raise RuntimeError("storage_root is unavailable")
    if thread_store is not None:
        thread_location = str(thread_store.session_dir(runtime.thread_id))
    metadata = {
        "thread_id": runtime.thread_id,
        "project": getattr(item, "project", None),
        "cwd": getattr(item, "cwd", None),
        "thread_location": thread_location,
    }
    saved: SavedReport = ReportStore(storage_value).save(
        title=report_title,
        final_report=result.final_report,
        state=state,
        metadata=metadata,
    )
    return ReportArtifact(
        final_report=result.final_report,
        artifact_path=None,
        report_path=saved.report_path,
        conversation_path=saved.conversation_path,
        title=saved.title,
        thread_id=runtime.thread_id,
    )
