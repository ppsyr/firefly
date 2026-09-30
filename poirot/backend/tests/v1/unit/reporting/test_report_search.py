from pathlib import Path

import pytest

from poirot.backend.agents.reporting.report_index import ReportIndex
from poirot.backend.agents.reporting.report_search import (
    format_search_result,
    parse_search_command,
    search_reports,
)
from poirot.backend.agents.reporting.report_store import ReportStore


def _runtime(root: Path, project: str | None = "demo", cwd: str | None = None):
    item = type("Item", (), {"project": project, "cwd": cwd})()
    store = type("Store", (), {"require": lambda self, _: item})()
    config = type("Config", (), {"runtime": type("RuntimeConfig", (), {"storage_root": str(root)})()})()
    return type("Runtime", (), {"thread_id": "current", "thread_store": store, "config": config})()


def _save(root: Path, title: str, project: str, cwd: str, marker: str):
    return ReportStore(root).save(
        title=title,
        final_report=marker,
        state={
            "research_question": "历史报告",
            "messages": [
                {"role": "user", "content": f"询问 {marker}"},
                {"role": "assistant", "content": f"回答 {marker}"},
            ],
            "observations": [{"content": marker}],
        },
        metadata={"thread_id": title, "project": project, "cwd": cwd},
    )


def test_parse_search_command_and_special_text():
    assert parse_search_command('--all-projects "FTS5 (中文)"') == ("FTS5 (中文)", True)
    with pytest.raises(ValueError):
        parse_search_command("--unknown x")
    with pytest.raises(ValueError):
        parse_search_command("   ")


def test_default_scope_and_explicit_all_projects(tmp_path: Path):
    cwd = tmp_path / "project"
    cwd.mkdir()
    _save(tmp_path, "local", "demo", str(cwd), "shared_marker")
    _save(tmp_path, "other", "other", str(tmp_path / "other"), "shared_marker")
    ReportIndex(tmp_path).rebuild()
    local = search_reports(_runtime(tmp_path, "demo", str(cwd)), "shared_marker")
    assert len(local.reports) == 1
    all_results = search_reports(_runtime(tmp_path, "demo", str(cwd)), "--all-projects shared_marker")
    assert len(all_results.reports) == 2


def test_missing_scope_and_progressive_layers(tmp_path: Path):
    result = search_reports(_runtime(tmp_path, None, None), "anything")
    assert result.message and "cwd" in result.message
    cwd = tmp_path / "project"
    cwd.mkdir()
    saved = _save(tmp_path, "deep", "demo", str(cwd), "only_l2_marker")
    ReportIndex(tmp_path).index_report(saved.report_path)
    result = search_reports(_runtime(tmp_path, "demo", str(cwd)), "only_l2_marker")
    assert result.reports
    blocks = result.reports[0][1]
    assert any(block.level == "L0" for block in blocks)
    assert any(block.level == "L2" for block in blocks)
    output = format_search_result(result, tmp_path)
    assert "[L2 expanded]" in output


def test_missing_index_has_distinct_feedback(tmp_path: Path):
    result = search_reports(_runtime(tmp_path, "demo", str(tmp_path)), "anything")
    assert result.message and "索引尚未建立" in result.message
