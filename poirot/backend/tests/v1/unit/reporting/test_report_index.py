"""SQLite FTS5 report index tests."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from poirot.backend.agents.reporting.report_index import ReportIndex
from poirot.backend.agents.reporting.report_store import ReportStore


def _save(root: Path, title: str, *, project: str = "demo", cwd: str | None = None, text: str = "中文持久化报告 with config_key") -> str:
    return ReportStore(root).save(
        title=title,
        final_report=text,
        state={
            "research_question": "报告检索问题",
            "messages": [{"role": "user", "content": "总结"}, {"role": "assistant", "content": "完成"}],
            "observations": [{"content": text}],
        },
        metadata={"thread_id": f"thread-{title}", "project": project, "cwd": cwd},
        created_at=datetime(2026, 1, 2, 3, 4, 5).astimezone(),
    ).report_path


def test_index_searches_layers_and_excludes_code_and_conversation(tmp_path: Path):
    path = _save(tmp_path, "索引报告")
    report = Path(path)
    # Keep the valid rag block and add an unrelated code fence to L1.
    text = report.read_text(encoding="utf-8")
    text = text.replace("### Key decisions and constraints\n", "### Key decisions and constraints\n```python\nsecret_tool_output\n```\n")
    report.write_text(text, encoding="utf-8")
    index = ReportIndex(tmp_path)
    index.index_report(report)
    assert {row.level for row in index.search("持久化") } >= {"L0", "L1", "L2"}
    assert index.search("secret_tool_output") == []
    assert index.search("config_key")[0].report_path in str(report.relative_to(tmp_path / "reports"))


def test_duplicate_index_and_changed_report_replace_blocks(tmp_path: Path):
    path = _save(tmp_path, "重复")
    index = ReportIndex(tmp_path)
    index.index_report(path)
    first = index.search("config_key")
    index.index_report(path)
    assert len(index.search("config_key")) == len(first)
    report = Path(path)
    report.write_text(report.read_text(encoding="utf-8").replace("config_key", "new_identifier"), encoding="utf-8")
    index.index_report(path)
    assert index.search("config_key") == []
    assert index.search("new_identifier")


def test_project_and_normalized_cwd_filters(tmp_path: Path):
    cwd = tmp_path / "project"
    cwd.mkdir()
    one = _save(tmp_path, "一", project="a", cwd=str(cwd), text="shared 中文")
    _save(tmp_path, "二", project="b", cwd=str(tmp_path / "other"), text="shared 中文")
    index = ReportIndex(tmp_path)
    index.rebuild()
    assert {row.project for row in index.search("shared", project="a")} == {"a"}
    assert {row.report_path for row in index.search("shared", cwd=cwd / ".")} == {Path(one).relative_to(tmp_path / "reports").as_posix()}
    assert index.search("shared", cwd=tmp_path / "project-child") == []


def test_rebuild_skips_bad_files_and_removes_deleted_reports(tmp_path: Path):
    good = _save(tmp_path, "好", text="rebuild_marker")
    bad = tmp_path / "reports" / "2026" / "01" / "02" / "report-bad"
    bad.mkdir(parents=True)
    (bad / "report-bad.md").write_text("not frontmatter", encoding="utf-8")
    index = ReportIndex(tmp_path)
    result = index.rebuild()
    assert result.indexed == 1
    assert any("report-bad.md" in path for path, _ in result.skipped)
    assert index.search("rebuild_marker")
    Path(good).unlink()
    index.rebuild()
    assert index.search("rebuild_marker") == []


def test_special_and_empty_queries_are_stable(tmp_path: Path):
    path = _save(tmp_path, "查询", text="FTS5 (safe) 中文")
    index = ReportIndex(tmp_path)
    index.index_report(path)
    assert index.search("   ") == []
    assert index.search('FTS5 (safe) "中文"')
    assert index.search("does-not-exist") == []
