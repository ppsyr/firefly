"""User-level manual report snapshot tests."""

import json
from pathlib import Path

import pytest

from poirot.backend.agents.reporting.report_store import ReportStore, sanitize_title


def _state():
    return {
        "research_question": "如何恢复 thread",
        "messages": [
            {"role": "system", "content": "hidden"},
            {"role": "user", "content": "请总结恢复方案"},
            {"role": "tool", "content": "secret tool output"},
            {"role": "assistant", "content": "已完成恢复方案"},
        ],
        "observations": [{"content": "每个 thread 使用独立 checkpoint 数据库。"}],
        "sources": [{"title": "设计文档", "url": "https://example.invalid/design"}],
    }


def test_save_creates_dated_pair_with_layers_and_metadata(tmp_path: Path):
    saved = ReportStore(tmp_path).save(
        title="恢复 / 方案 ..",
        final_report="# 结果\n已完成",
        state=_state(),
        metadata={
            "thread_id": "thread-1",
            "project": "demo",
            "cwd": str(tmp_path),
            "thread_location": str(tmp_path / "sessions"),
        },
    )

    report = Path(saved.report_path)
    conversation = Path(saved.conversation_path)
    assert report.parent.name == report.stem
    assert report.parent.parent.parent.parent.parent.name == "reports"
    assert report.name.endswith(".md")
    assert conversation.name == report.stem + ".conversation.jsonl"
    text = report.read_text(encoding="utf-8")
    for field in (
        "name",
        "description",
        "thread_id",
        "project",
        "cwd",
        "report_created_at",
        "thread_location",
        "conversation_location",
        "schema_version",
        "location",
    ):
        assert f"{field}:" in text
    assert "## L0 Overview" in text
    assert "## L1 Structured Summary" in text
    assert "```rag" in text
    rows = [json.loads(line) for line in conversation.read_text(encoding="utf-8").splitlines()]
    assert [(row["turn"], row["role"]) for row in rows] == [(1, "user"), (1, "assistant")]
    assert all("secret tool output" not in row["content"] for row in rows)


def test_same_second_title_does_not_overwrite(tmp_path: Path):
    from datetime import datetime

    when = datetime(2026, 1, 2, 3, 4, 5).astimezone()
    store = ReportStore(tmp_path)
    first = store.save(title="同名", final_report="one", state=_state(), metadata={}, created_at=when)
    second = store.save(title="同名", final_report="two", state=_state(), metadata={}, created_at=when)
    assert first.report_path != second.report_path
    assert Path(first.report_path).read_text(encoding="utf-8").find("one") >= 0
    assert Path(second.report_path).read_text(encoding="utf-8").find("two") >= 0
    assert Path(first.conversation_path).exists()
    assert Path(second.conversation_path).exists()


def test_empty_explicit_title_is_rejected():
    with pytest.raises(ValueError):
        sanitize_title("   ", fallback="")


def test_code_fences_are_not_promoted_to_rag(tmp_path: Path):
    state = {"messages": [], "observations": []}
    saved = ReportStore(tmp_path).save(
        title="代码",
        final_report="答案\n```python\nprint('secret')\n```",
        state=state,
        metadata={},
    )
    text = Path(saved.report_path).read_text(encoding="utf-8")
    rag = text.split("```rag", 1)[1].split("```", 1)[0]
    assert "print('secret')" not in rag


def test_pair_is_removed_when_finalization_fails(tmp_path: Path, monkeypatch):
    store = ReportStore(tmp_path)
    monkeypatch.setattr(store, "_fsync_directory", lambda path: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        store.save(title="失败", final_report="x", state=_state(), metadata={})
    assert not list((tmp_path / "reports").rglob("report-*.md"))
    assert not list((tmp_path / "reports").rglob("*.conversation.jsonl"))
