from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import warnings
import sqlite3
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from langgraph.graph import START, StateGraph
from typing import TypedDict

from poirot.backend.agents.runtime.checkpointer import SQLiteCheckpointer, SessionCheckpointer
from poirot.backend.agents.runtime.threads import ThreadStore, validate_thread_id
from poirot.backend.app.bootstrap import AppRuntime
from poirot.backend.agents.config.loader import load_config
from poirot.backend.agents.journal.run_journal import RunJournal
from poirot.backend.agents.runtime.run_manager import RunManager


class State(TypedDict):
    count: int
    label: str


def _graph(saver):
    builder = StateGraph(State)
    builder.add_node("advance", lambda state: {"count": state["count"] + 1, "label": state["label"]})
    builder.add_edge(START, "advance")
    return builder.compile(checkpointer=saver)


def test_state_isolation_and_delete(tmp_path):
    saver = SQLiteCheckpointer(tmp_path / "checkpoints.db")
    graph = _graph(saver)
    config_a = {"configurable": {"thread_id": "a"}}
    config_b = {"configurable": {"thread_id": "b"}}
    try:
        assert asyncio.run(graph.ainvoke({"count": 0, "label": "A"}, config_a))["count"] == 1
        assert asyncio.run(graph.ainvoke({"count": 1, "label": "A"}, config_a))["count"] == 2
        assert saver.get_tuple(config_b) is None
        assert asyncio.run(graph.ainvoke({"count": 0, "label": "B"}, config_b))["label"] == "B"
        assert saver.get_tuple(config_a).checkpoint["channel_values"]["count"] == 2
        saver.delete_thread("a")
        assert saver.get_tuple(config_a) is None
        assert saver.get_tuple(config_b) is not None
        with sqlite3.connect(tmp_path / "checkpoints.db") as conn:
            assert conn.execute("SELECT count(*) FROM checkpoints WHERE thread_id='a'").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM writes WHERE thread_id='a'").fetchone()[0] == 0
    finally:
        saver.close()


def test_thread_metadata_and_titles(tmp_path):
    store = ThreadStore(tmp_path)
    item = store.create()
    assert item.title.endswith("新会话")
    assert item.created_at == item.updated_at
    assert store.get(item.thread_id) == item
    first = store.update(item.thread_id, first_message="中文  hello\n\t world " + "字" * 70)
    assert first.title.endswith("中文 hello world " + "字" * 45)
    assert len(first.title.split(" ", 2)[-1]) == 60
    assert store.update(item.thread_id, first_message="another").title == first.title
    renamed = store.update(item.thread_id, title="  My\n session\x1b  ")
    assert renamed.title == "My session"
    assert store.update(item.thread_id, first_message="next").title == renamed.title
    assert renamed.created_at == item.created_at
    before_first = store.create("before-first")
    store.update(before_first.thread_id, title="My manual title")
    assert store.update(before_first.thread_id, first_message="This must not replace it").title == "My manual title"
    with pytest.raises(ValueError):
        store.update(item.thread_id, title="\t ")
    with pytest.raises(ValueError):
        validate_thread_id("../outside")


def test_corrupt_metadata_is_preserved(tmp_path):
    store = ThreadStore(tmp_path)
    valid = store.create()
    bad = store.root / "2026" / "09" / "28" / "thread-00-00-00-broken" / "metadata.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("{", encoding="utf-8")
    with warnings.catch_warnings(record=True) as records:
        assert store.list() == [valid]
    assert "metadata.json" in str(records[0].message)
    assert bad.read_text(encoding="utf-8") == "{"


def test_atomic_metadata_and_delete_retry(tmp_path, monkeypatch):
    store = ThreadStore(tmp_path)
    current = store.create("current")
    other = store.create("other")
    original = store._path(current.thread_id).read_bytes()
    def fail_replace(*args):
        raise OSError("disk failure")
    monkeypatch.setattr("poirot.backend.agents.runtime.threads.os.replace", fail_replace)
    with pytest.raises(OSError):
        store.update(current.thread_id, title="new")
    assert store._path(current.thread_id).read_bytes() == original
    monkeypatch.undo()

    saver = SQLiteCheckpointer(tmp_path / "checkpoints.db")
    config = load_config(cli_overrides={"storage_root": str(tmp_path), "logs_root": str(tmp_path / "logs")})
    runtime = AppRuntime(config, None, RunManager(config), "fake", current.thread_id, tmp_path / "logs" / "threads" / current.thread_id, RunJournal(current.thread_id, tmp_path / "events.jsonl"), None, thread_store=store, checkpointer=saver)
    try:
        with pytest.raises(ValueError, match="current"):
            runtime.delete_thread(current.thread_id)
        original_db_delete = saver.delete_thread
        def fail_db_delete(tid):
            raise OSError("database unavailable")
        monkeypatch.setattr(saver, "delete_thread", fail_db_delete)
        with pytest.raises(OSError, match="database"):
            runtime.delete_thread(other.thread_id)
        assert store.require(other.thread_id) == other
        monkeypatch.setattr(saver, "delete_thread", original_db_delete)
        original_delete = store.delete
        def fail_delete(tid):
            raise OSError("metadata unavailable")
        monkeypatch.setattr(store, "delete", fail_delete)
        with pytest.raises(RuntimeError, match="incomplete"):
            runtime.delete_thread(other.thread_id)
        assert store.require(other.thread_id) == other
        monkeypatch.setattr(store, "delete", original_delete)
        runtime.delete_thread(other.thread_id)
        assert store.get(other.thread_id) is None
    finally:
        runtime.close()


def test_metadata_failure_prevents_graph_submission(tmp_path, monkeypatch):
    store = ThreadStore(tmp_path)
    store.create("current")
    saver = SQLiteCheckpointer(tmp_path / "checkpoints.db")
    config = load_config(cli_overrides={"storage_root": str(tmp_path), "logs_root": str(tmp_path / "logs")})
    class NoGraph:
        def run(self, *args, **kwargs):
            raise AssertionError("graph must not run")
    runtime = AppRuntime(config, None, RunManager(config), "fake", "current", tmp_path / "logs" / "threads" / "current", RunJournal("current", tmp_path / "events.jsonl"), NoGraph(), thread_store=store, checkpointer=saver)
    original = store.update
    def fail_start(tid, **kwargs):
        if "first_message" in kwargs:
            raise OSError("metadata unavailable")
        return original(tid, **kwargs)
    monkeypatch.setattr(store, "update", fail_start)
    try:
        with pytest.raises(OSError, match="metadata"):
            runtime.run_question("hello")
        assert saver.get_tuple({"configurable": {"thread_id": "current"}}) is None
        assert not runtime.active_threads
    finally:
        runtime.close()


def test_corrupt_checkpoint_rejects_switch_without_changing_runtime(tmp_path):
    store = ThreadStore(tmp_path)
    store.create("current")
    store.create("corrupt")
    saver = SQLiteCheckpointer(tmp_path / "checkpoints.db")
    graph = _graph(saver)
    asyncio.run(graph.ainvoke({"count": 0, "label": "bad"}, {"configurable": {"thread_id": "corrupt"}}))
    with sqlite3.connect(tmp_path / "checkpoints.db") as conn:
        conn.execute("UPDATE checkpoints SET type='unknown-format' WHERE thread_id='corrupt'")
    config = load_config(cli_overrides={"storage_root": str(tmp_path), "logs_root": str(tmp_path / "logs")})
    runtime = AppRuntime(config, None, RunManager(config), "fake", "current", tmp_path / "logs" / "threads" / "current", RunJournal("current", tmp_path / "events.jsonl"), None, thread_store=store, checkpointer=saver)
    try:
        with pytest.raises((ValueError, NotImplementedError)):
            runtime.switch_thread("corrupt")
        assert runtime.thread_id == "current"
        assert saver.get_tuple({"configurable": {"thread_id": "current"}}) is None
        assert store.require("corrupt").thread_id == "corrupt"
    finally:
        runtime.close()


def test_app_runtime_switches_without_copying_state(tmp_path):
    from poirot.backend.tests.v1._fake_model import FakeChatModelWithTools
    from poirot.backend.agents.config.model_router import ModelRouter
    with patch.object(ModelRouter, "build_model", lambda self, role: FakeChatModelWithTools(responses=["answer"])), patch.object(ModelRouter, "chain_names", lambda self, role: ["fake"]), patch("poirot.backend.app.bootstrap._check_node_available", return_value=False), patch("poirot.backend.app.bootstrap.load_multiagent_config") as ma_config:
        ma_config.return_value.enabled = False
        runtime = __import__("poirot.backend.app.bootstrap", fromlist=["bootstrap_runtime"]).bootstrap_runtime(cli_overrides={"storage_root": str(tmp_path / "user"), "logs_root": str(tmp_path / "logs")})
    try:
        first_id = runtime.thread_id
        runtime.run_question("first")
        runtime.run_question("second")
        second = runtime.new_thread()
        assert second.thread_id != first_id
        assert second.checkpointer is runtime.checkpointer
        assert second.checkpointer.get_tuple({"configurable": {"thread_id": second.thread_id}}) is None
        result_b = second.run_question("other")
        assert [m.content for m in result_b.state["messages"] if m.type == "human"] == ["other"]
        restored = second.switch_thread(first_id)
        result_a = restored.run_question("third")
        assert [m.content for m in result_a.state["messages"] if m.type == "human"] == ["first", "second", "third"]
        assert second.checkpointer.get_tuple({"configurable": {"thread_id": second.thread_id}}) is not None
        changed_mode = restored.switch_expert_mode(False)
        assert changed_mode.checkpointer is runtime.checkpointer
        assert changed_mode.config.runtime.storage_root == runtime.config.runtime.storage_root
        assert changed_mode.config.runtime.logs_root == runtime.config.runtime.logs_root
    finally:
        runtime.close()
        runtime.close()
    with pytest.raises(RuntimeError, match="closed"):
        runtime.checkpointer.get_tuple({"configurable": {"thread_id": first_id}})


_APP_PROCESS = r'''
import json, os, sys
from unittest.mock import patch
from poirot.backend.tests.v1._fake_model import FakeChatModelWithTools
from poirot.backend.agents.config.model_router import ModelRouter
from poirot.backend.app.bootstrap import bootstrap_runtime

def fake_model(self, role):
    return FakeChatModelWithTools(responses=["deterministic answer"])

with patch.object(ModelRouter, "build_model", fake_model), patch.object(ModelRouter, "chain_names", lambda self, role: ["fake"]), patch("poirot.backend.app.bootstrap._check_node_available", return_value=False):
    runtime = bootstrap_runtime(cli_overrides={"storage_root": os.environ["POIROT_STORAGE_ROOT"], "logs_root": os.environ["TEST_LOGS"]})
    try:
        thread_id = "fixed-thread"
        runtime.thread_store.get(thread_id) or runtime.thread_store.create(thread_id)
        runtime = runtime.switch_thread(thread_id)
        before = runtime.thread_store.require(thread_id)
        state = runtime.checkpointer.get_tuple({"configurable": {"thread_id": thread_id}})
        if sys.argv[1] == "second":
            assert state is not None
            messages = state.checkpoint["channel_values"]["messages"]
            assert [m.content for m in messages if m.type == "human"] == ["first question"]
        result = runtime.run_question("first question" if sys.argv[1] == "first" else "second question")
        after = runtime.thread_store.require(thread_id)
        messages = result.state["messages"]
        print(json.dumps({"id": thread_id, "created": after.created_at, "title": after.title, "humans": [m.content for m in messages if m.type == "human"], "answer": result.final_report}))
    finally:
        runtime.close()
'''


def test_actual_app_bootstrap_across_processes(tmp_path):
    env = {**os.environ, "POIROT_STORAGE_ROOT": str(tmp_path / "user"), "TEST_LOGS": str(tmp_path / "logs"), "POIROT_MULTIAGENT_ENABLED": "false", "POIROT_SKILL_ENABLED": "false", "POIROT_MEMORY_USE": "", "POIROT_MCP_ENABLED": "false"}
    def run(phase):
        proc = subprocess.run([sys.executable, "-c", _APP_PROCESS, phase], cwd=Path(__file__).parents[5], env=env, capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return json.loads(proc.stdout.strip().splitlines()[-1])
    first = run("first")
    second = run("second")
    assert first["created"] == second["created"]
    assert first["title"] == second["title"]
    assert second["humans"] == ["first question", "second question"]
    assert second["answer"] == "deterministic answer"
    metadata = list((tmp_path / "user" / "sessions").glob("*/*/*/thread-*-fixed-thread/metadata.json"))
    assert len(metadata) == 1
    assert (metadata[0].parent / "checkpoints.db").is_file()
    assert (metadata[0].parent / "thread-events.jsonl").is_file()
    assert len(list((metadata[0].parent / "runs").glob("*/record.json"))) == 2
    assert not (tmp_path / "user" / "checkpoints.db").exists()


def test_default_sessions_follow_home_and_ignore_cwd(tmp_path, monkeypatch):
    home = tmp_path / "another-user"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("POIROT_STORAGE_ROOT", raising=False)
    store = ThreadStore()
    item = store.create("portable")
    created = datetime.fromisoformat(item.created_at).astimezone()
    directory = store.session_dir(item.thread_id)
    assert directory == home / ".poirot" / "sessions" / created.strftime("%Y/%m/%d") / f"thread-{created:%H-%M-%S}-portable"
    monkeypatch.chdir(tmp_path)
    reopened = ThreadStore()
    assert reopened.require("portable") == item
    reopened.update("portable", title="renamed")
    assert reopened.session_dir("portable") == directory


def test_session_ids_with_common_suffix_and_updates_across_dates(tmp_path, monkeypatch):
    store = ThreadStore(tmp_path)
    monkeypatch.setattr("poirot.backend.agents.runtime.threads._now", lambda: "2026-01-01T10:00:00+00:00")
    first = store.create("x-foo")
    second = store.create("foo")
    directory = store.session_dir("foo")
    assert store.get("x-foo") == first
    assert store.get("foo") == second
    monkeypatch.setattr("poirot.backend.agents.runtime.threads._now", lambda: "2026-02-02T10:00:00+00:00")
    store.update("foo", title="Later title")
    assert store.session_dir("foo") == directory
    assert len(store.list()) == 2
    store.delete("foo")
    assert store.require("x-foo") == first
    assert store.get("foo") is None


def test_migrate_legacy_checkpoints_and_pending_writes(tmp_path):
    root = tmp_path / "user"
    legacy_metadata = root / "threads" / "legacy.json"
    legacy_metadata.parent.mkdir(parents=True)
    data = {"thread_id": "legacy", "title": "Manual title", "created_at": "2025-12-31T23:30:00+00:00", "updated_at": "2026-01-02T00:00:00+00:00", "title_set": True}
    legacy_metadata.write_text(json.dumps(data), encoding="utf-8")
    legacy = SQLiteCheckpointer(root / "checkpoints.db")
    config = {"configurable": {"thread_id": "legacy"}}
    other_config = {"configurable": {"thread_id": "other"}}
    graph = _graph(legacy)
    graph.invoke({"count": 1, "label": "saved"}, config)
    graph.invoke({"count": 10, "label": "other"}, other_config)
    checkpoint = legacy.get_tuple(config)
    legacy.put_writes(checkpoint.config, [("pending", "unfinished")], "pending-task")
    legacy.close()

    store = ThreadStore(root)
    migrated = store.require("legacy")
    assert migrated.title == data["title"]
    assert migrated.created_at == data["created_at"]
    assert not legacy_metadata.exists()
    directory = store.session_dir("legacy")
    saver = SessionCheckpointer(store)
    try:
        restored = saver.get_tuple(config)
        assert restored.checkpoint == checkpoint.checkpoint
        assert ("pending-task", "pending", "unfinished") in restored.pending_writes
        assert _graph(saver).invoke(None, config)["count"] == 2
        with sqlite3.connect(directory / "checkpoints.db") as conn:
            assert conn.execute("SELECT DISTINCT thread_id FROM checkpoints").fetchall() == [("legacy",)]
        saver.delete_thread("legacy")
        store.delete("legacy")
    finally:
        saver.close()
    assert store.get("legacy") is None
    with sqlite3.connect(root / "checkpoints.db") as conn:
        assert conn.execute("SELECT count(*) FROM checkpoints WHERE thread_id='legacy'").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM writes WHERE thread_id='legacy'").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM checkpoints WHERE thread_id='other'").fetchone()[0] > 0


def test_extra_reference_dirs_are_thread_scoped_and_persisted(tmp_path):
    root = tmp_path / "storage"
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    store = ThreadStore(root)
    first = store.create("first")
    second = store.create("second")
    store.add_extra_dir(first.thread_id, first_dir)
    link = tmp_path / "first-link"
    link.symlink_to(first_dir, target_is_directory=True)
    with pytest.raises(FileExistsError):
        store.add_extra_dir(first.thread_id, link)
    assert store.require(first.thread_id).extra_dirs == (str(first_dir.resolve()),)
    assert store.require(second.thread_id).extra_dirs == ()

    restored = ThreadStore(root)
    assert restored.require(first.thread_id).extra_dirs == (str(first_dir.resolve()),)
    restored.remove_extra_dir(first.thread_id, link)
    assert restored.require(first.thread_id).extra_dirs == ()
