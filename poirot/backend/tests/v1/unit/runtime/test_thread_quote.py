from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from poirot.backend.agents.runtime.thread_quote import (
    ThreadQuoteAmbiguous,
    ThreadQuoteUnavailable,
    quote_threads,
    thread_candidates,
    thread_reference_fragment_at_cursor,
)
from poirot.backend.agents.runtime.threads import ThreadStore


class FakeCheckpointer:
    def __init__(self, checkpoints):
        self.checkpoints = checkpoints
        self.requested = []

    def get_tuple(self, config):
        thread_id = config["configurable"]["thread_id"]
        self.requested.append(thread_id)
        return self.checkpoints.get(thread_id)


def _store(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    store = ThreadStore(tmp_path / "storage")
    project = store.projects.ensure(root, "first")
    current = store.create("current", project=project.project_name, cwd=project.dir)
    target = store.create("abc123", project=project.project_name, cwd=project.dir)
    return store, current, target, root


def _checkpoint(*messages):
    return SimpleNamespace(checkpoint={"channel_values": {"messages": list(messages)}})


def test_quote_reads_latest_checkpoint_and_filters_roles(tmp_path):
    store, current, target, _ = _store(tmp_path)
    checkpoint = _checkpoint(
        SystemMessage(content="secret system"),
        HumanMessage(content="old question"),
        AIMessage(content="old answer"),
        AIMessage(content="", tool_calls=[{"name": "search", "args": {"secret": 1}, "id": "1"}]),
        ToolMessage(content="tool result", tool_call_id="1"),
        HumanMessage(content=[{"type": "text", "text": "new question"}, {"type": "image_url", "url": "x"}]),
        AIMessage(content=[{"type": "text", "text": "new answer"}, {"type": "image_url", "url": "y"}]),
    )
    cp = FakeCheckpointer({target.thread_id: checkpoint})
    result = quote_threads(
        f"@thread-{target.thread_id} continue",
        current=current,
        thread_store=store,
        checkpointer=cp,
    )
    assert cp.requested == [target.thread_id]
    rendered = result.enriched_suffix
    assert "old question" in rendered and "new answer" in rendered
    assert "secret system" not in rendered
    assert "tool result" not in rendered
    assert "secret" not in rendered
    assert result.masked_question.startswith("               ")
    assert result.quotes[0].thread_id == target.thread_id


def test_same_directory_and_title_candidates_are_bounded(tmp_path):
    store, current, target, root = _store(tmp_path)
    other_root = tmp_path / "other"
    other_root.mkdir()
    other_project = store.projects.ensure(other_root, "second")
    other = store.create("abc999", project=other_project.project_name, cwd=other_project.dir)
    store.update(target.thread_id, title="Planning Session")
    candidates = thread_candidates("", current=current, thread_store=store)
    assert [item.thread_id for item in candidates] == [target.thread_id]
    assert all(item.thread_id != other.thread_id for item in candidates)
    assert thread_reference_fragment_at_cursor('@"Planning') == ("Planning", 0, True)
    assert thread_reference_fragment_at_cursor("@abc") == ("abc", 0, False)


def test_ambiguous_short_id_and_missing_history_do_not_inject(tmp_path):
    store, current, target, root = _store(tmp_path)
    second = store.create("abc999", project=current.project, cwd=current.cwd)
    store.update(target.thread_id, title="Unique target")
    cp = FakeCheckpointer({})
    with pytest.raises(ThreadQuoteAmbiguous, match="Candidates"):
        quote_threads("@abc", current=current, thread_store=store, checkpointer=cp)
    with pytest.raises(ThreadQuoteUnavailable, match="no committed checkpoint"):
        quote_threads(f'@"{target.title}"', current=current, thread_store=store, checkpointer=cp)


def test_budget_keeps_latest_turns_and_marks_truncation(tmp_path):
    store, current, target, _ = _store(tmp_path)
    messages = []
    for index in range(15):
        messages.extend([HumanMessage(content=f"question-{index} " + "x" * 300), AIMessage(content=f"answer-{index} " + "y" * 300)])
    cp = FakeCheckpointer({target.thread_id: _checkpoint(*messages)})
    result = quote_threads(
        f"@{target.thread_id} summarize",
        current=current,
        thread_store=store,
        checkpointer=cp,
    )
    rendered = result.enriched_suffix
    assert result.quotes[0].truncated is True
    assert "question-14" in rendered and "answer-14" in rendered
    assert "question-0" not in rendered
    assert "truncated" in rendered


def test_unbound_thread_does_not_reject_plain_at_mentions(tmp_path):
    store = ThreadStore(tmp_path / "storage")
    current = store.create("current")
    cp = FakeCheckpointer({})
    result = quote_threads(
        "contact @support about this",
        current=current,
        thread_store=store,
        checkpointer=cp,
    )
    assert result.quotes == ()
    with pytest.raises(ThreadQuoteUnavailable, match="not bound"):
        quote_threads(
            '@"Missing title"',
            current=current,
            thread_store=store,
            checkpointer=cp,
        )
