"""generate_report_from_thread 测试：渠道无关报告生成服务。"""

from pathlib import Path
from types import SimpleNamespace


def _fake_runtime(tmp_path: Path, *, state: dict, save_artifact: bool = True) -> SimpleNamespace:
    """构造满足 _ReportRuntime Protocol 的 fake runtime。"""
    snapshot = SimpleNamespace(values=state)
    graph = SimpleNamespace(get_state=lambda config: snapshot)
    reporter = SimpleNamespace(
        generate_report=lambda s, run_context=None: SimpleNamespace(final_report="# 报告\n正文"),
    )
    artifact_store = SimpleNamespace(
        save_artifact=lambda content, output_dir, title, filename, metadata: SimpleNamespace(
            path=str(Path(output_dir) / filename),
        ),
    )
    registry = SimpleNamespace(get_reporter=lambda: reporter, get_artifact_store=lambda: artifact_store)
    config = SimpleNamespace(reporting=SimpleNamespace(save_artifact=save_artifact))
    return SimpleNamespace(
        leader_agent=SimpleNamespace(graph=graph),
        thread_id="thread-test",
        capability_registry=registry,
        thread_dir=tmp_path,
        config=config,
    )


def test_generate_report_from_thread_returns_final_report(tmp_path) -> None:
    runtime = _fake_runtime(tmp_path, state={"observations": [{"content": "obs1"}]})
    from poirot.backend.agents.reporting import generate_report_from_thread

    result = generate_report_from_thread(runtime=runtime)
    assert result.final_report == "# 报告\n正文"


def test_generate_report_from_thread_saves_artifact(tmp_path) -> None:
    runtime = _fake_runtime(tmp_path, state={"observations": []})
    from poirot.backend.agents.reporting import generate_report_from_thread

    result = generate_report_from_thread(runtime=runtime)
    assert result.artifact_path is not None
    assert result.artifact_path.endswith("report.md")


def test_generate_report_from_thread_no_artifact_when_disabled(tmp_path) -> None:
    runtime = _fake_runtime(tmp_path, state={"observations": []}, save_artifact=False)
    from poirot.backend.agents.reporting import generate_report_from_thread

    result = generate_report_from_thread(runtime=runtime)
    assert result.artifact_path is None


def test_generate_report_from_thread_topic_overrides_research_question(tmp_path) -> None:
    runtime = _fake_runtime(tmp_path, state={"research_question": "原问题"})
    captured = {}
    runtime.capability_registry.get_reporter = lambda: SimpleNamespace(
        generate_report=lambda s, run_context=None: (
            captured.update({"rq": s.get("research_question")}),
            SimpleNamespace(final_report="x"),
        )[1],
    )
    from poirot.backend.agents.reporting import generate_report_from_thread

    generate_report_from_thread(runtime=runtime, topic="自定义主题")
    assert captured["rq"] == "自定义主题"


def test_generate_report_from_thread_empty_state_still_works(tmp_path) -> None:
    """无 checkpoint（snapshot.values 空）→ state 空 dict → reporter fallback。"""
    runtime = _fake_runtime(tmp_path, state={})
    from poirot.backend.agents.reporting import generate_report_from_thread

    result = generate_report_from_thread(runtime=runtime)
    assert result.final_report == "# 报告\n正文"


def test_generate_report_from_thread_none_snapshot(tmp_path) -> None:
    """graph.get_state 返回 None（无 checkpoint）→ state 空。"""
    runtime = _fake_runtime(tmp_path, state={})
    runtime.leader_agent.graph.get_state = lambda config: None
    from poirot.backend.agents.reporting import generate_report_from_thread

    result = generate_report_from_thread(runtime=runtime)
    assert result.final_report  # 非 None（reporter fallback）


def test_persist_report_from_thread_uses_user_storage(tmp_path) -> None:
    from types import SimpleNamespace

    state = {
        "research_question": "测试问题",
        "messages": [
            {"role": "user", "content": "做总结"},
            {"role": "assistant", "content": "总结完成"},
        ],
        "observations": [{"content": "稳定事实"}],
    }
    graph = SimpleNamespace(get_state=lambda config: SimpleNamespace(values=state))
    reporter = SimpleNamespace(generate_report=lambda s, run_context=None: SimpleNamespace(final_report="结果"))
    item = SimpleNamespace(thread_id="thread-test", title="线程标题", project="demo", cwd=str(tmp_path))
    store = SimpleNamespace(
        require=lambda thread_id: item,
        session_dir=lambda thread_id: tmp_path / "sessions" / thread_id,
    )
    runtime = SimpleNamespace(
        leader_agent=SimpleNamespace(graph=graph),
        thread_id="thread-test",
        thread_store=store,
        capability_registry=SimpleNamespace(get_reporter=lambda: reporter),
        config=SimpleNamespace(runtime=SimpleNamespace(storage_root=str(tmp_path / "user-store"))),
    )
    from poirot.backend.agents.reporting import persist_report_from_thread

    result = persist_report_from_thread(runtime)
    assert result.artifact_path is None
    assert result.title == "线程标题"
    assert result.report_path.startswith(str(tmp_path / "user-store" / "reports"))
    assert Path(result.report_path).exists()
    assert Path(result.conversation_path).exists()
