"""集成测试：AppRuntime 每轮为 search_reports 组装实时检索范围。

覆盖模块四的范围边界：
- 范围来自当前 thread 元数据（project / 规范化 cwd），不是进程 cwd 或上个 thread；
- 跨项目授权只看原始用户问题；
- thread 切换后下一次提问使用新 thread 的范围。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from poirot.backend.agents.artifacts.local_store import LocalArtifactStore
from poirot.backend.agents.capabilities.registry import CapabilityRegistry
from poirot.backend.agents.config.loader import load_config
from poirot.backend.agents.journal.run_journal import RunJournal
from poirot.backend.agents.reporting.markdown_reporter import MarkdownReporter
from poirot.backend.agents.runtime.checkpointer import SessionCheckpointer
from poirot.backend.agents.runtime.projects import ProjectStore
from poirot.backend.agents.runtime.run_manager import RunManager
from poirot.backend.agents.runtime.threads import ThreadStore
from poirot.backend.app.bootstrap import AppRuntime
from poirot.backend.tests.v1._fake_model import FakeChatModelWithTools


class _RecordingAgent:
    """Leader agent stand-in that records the run keywords it receives."""

    def __init__(self) -> None:
        self.search_scopes: list[dict] = []

    def run(self, question, run_context, **kwargs):
        self.search_scopes.append(kwargs.get("search_scope"))
        return SimpleNamespace(run_id="run", thread_id=run_context.thread_id, final_report="ok",
                                events_path="", artifact_path=None, state={})


def _runtime(root: Path) -> AppRuntime:
    storage_root = root / "storage"
    project_dir = root / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    config = load_config(expert_mode=False, cli_overrides={
        "logs_root": str(root / "logs"),
        "storage_root": str(storage_root),
    })
    model = FakeChatModelWithTools(responses=["回答"])
    registry = CapabilityRegistry(
        models={"researcher": model, "reporter": model},
        tools={},
        reporter=MarkdownReporter(),
        artifact_store=LocalArtifactStore(),
    )
    thread_store = ThreadStore(storage_root)
    project = ProjectStore(storage_root).ensure(project_dir)
    item = thread_store.create(project=project.project_name, cwd=project.dir)
    thread_dir = thread_store.session_dir(item.thread_id)
    agent = _RecordingAgent()
    return AppRuntime(
        config=config,
        capability_registry=registry,
        run_manager=RunManager(config),
        researcher_model_name="fake-test",
        thread_id=item.thread_id,
        thread_dir=thread_dir,
        thread_journal=RunJournal(item.thread_id, thread_dir / "thread-events.jsonl"),
        leader_agent=agent,
        thread_store=thread_store,
        checkpointer=SessionCheckpointer(thread_store),
        project=project,
        project_store=ProjectStore(storage_root),
    )


def test_run_question_passes_thread_scope_and_blocks_self_authorization(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime.run_question(question="之前讨论过的结论是什么？", run_id="run-a")
    scope = runtime.leader_agent.search_scopes[-1]
    assert scope["thread_id"] == runtime.thread_id
    assert scope["project"] == runtime.project.project_name
    assert scope["cwd"] == str(runtime.project.dir)
    assert scope["storage_root"] == runtime.config.runtime.storage_root
    assert scope["all_projects_allowed"] is False


def test_cross_project_authorization_comes_from_the_user_message(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime.run_question(question="把所有项目里之前的结论都查一下", run_id="run-b")
    assert runtime.leader_agent.search_scopes[-1]["all_projects_allowed"] is True
    runtime.run_question(question="总结一下刚才的结论", run_id="run-c")
    assert runtime.leader_agent.search_scopes[-1]["all_projects_allowed"] is False


def test_scope_follows_thread_switch(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime.run_question(question="历史结论", run_id="run-d")
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = ProjectStore(runtime.thread_store.storage_root).ensure(other_dir)
    item = runtime.thread_store.create(project=other.project_name, cwd=other.dir)
    switched = runtime.switch_thread(item.thread_id)
    try:
        switched.run_question(question="历史结论", run_id="run-e")
    finally:
        switched.checkpointer.close()
    first, second = switched.leader_agent.search_scopes
    assert first["project"] != second["project"]
    assert second["thread_id"] == item.thread_id
    assert second["project"] == other.project_name
    assert second["cwd"] == str(other.dir)


def test_stream_config_carries_the_scope_for_interactive_cli_and_tui(tmp_path: Path) -> None:
    """交互式 CLI / TUI 走 _build_stream_config，不经过 run_question。"""
    from poirot.backend.app.cli.main import _build_stream_config

    runtime = _runtime(tmp_path)
    runtime.ensure_thread_persisted()
    ctx = runtime.run_manager.create_run(
        thread_id=runtime.thread_id,
        user_id="default-user",
        model_name="fake-test",
        thread_dir=runtime.thread_dir,
        project=runtime.project.project_name,
        cwd=runtime.project.dir,
    )
    scope = _build_stream_config(runtime, ctx, "把之前report过哪些test都列出来")["configurable"]["report_search_scope"]
    assert scope["thread_id"] == runtime.thread_id
    assert scope["project"] == runtime.project.project_name
    assert scope["cwd"] == str(runtime.project.dir)
    assert scope["storage_root"] == runtime.config.runtime.storage_root
    assert scope["all_projects_allowed"] is False

    cross = _build_stream_config(runtime, ctx, "所有项目里之前讨论过的方案")["configurable"]["report_search_scope"]
    assert cross["all_projects_allowed"] is True