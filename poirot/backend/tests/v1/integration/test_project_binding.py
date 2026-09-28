from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console
from textual.app import App

from poirot.backend.agents.config.model_router import ModelRouter
from poirot.backend.agents.runtime.projects import ProjectStore
from poirot.backend.agents.runtime.threads import ThreadStore
from poirot.backend.app.bootstrap import bootstrap_runtime
from poirot.backend.app.cli.thread_selector import select_project, select_thread
from poirot.backend.app.tui.app import PoirotTUI
from poirot.backend.app.tui.project_selector import ProjectPicker
from poirot.backend.tests.v1._fake_model import FakeChatModelWithTools


def _runtime(root: Path, directory: Path | None, *, name: str | None = None):
    with (
        patch.object(
            ModelRouter,
            "build_model",
            lambda self, role: FakeChatModelWithTools(responses=["answer"]),
        ),
        patch.object(ModelRouter, "chain_names", lambda self, role: ["fake"]),
        patch("poirot.backend.app.bootstrap._check_node_available", return_value=False),
    ):
        return bootstrap_runtime(
            cli_overrides={"storage_root": str(root), "logs_root": str(root / "logs")},
            project_dir=directory,
            project_name=name,
        )


def test_project_directory_rules_and_conflicts(tmp_path, monkeypatch):
    root = tmp_path / "user"
    project = tmp_path / "demo"
    project.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(project, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    store = ProjectStore(root)
    assert store.ensure().dir == str(tmp_path.resolve())
    assert store.ensure("demo").project_name == "demo"
    assert store.ensure(alias).project_name == "demo"
    assert store.ensure(project, "demo").dir == str(project.resolve())
    with pytest.raises(ValueError, match="already bound"):
        store.ensure(project, "other")
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ValueError, match="already bound"):
        store.ensure(other, "demo")
    for name in (".", "..", "a.b", "a/b", "a\\b"):
        with pytest.raises(ValueError):
            store.ensure(other, name)
    with pytest.raises((ValueError, FileNotFoundError)):
        store.ensure("missing")
    file = tmp_path / "file"
    file.write_text("x")
    with pytest.raises(ValueError):
        store.ensure(file)


def test_default_directory_and_dir_do_not_change_sandbox_setup(tmp_path, monkeypatch):
    first_dir = tmp_path / "first"
    first_dir.mkdir()
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    monkeypatch.chdir(first_dir)
    captured = []

    def sandbox_setup(config):
        captured.append((Path.cwd(), config.sandbox, config.runtime.output_root))

    with patch(
        "poirot.backend.app.bootstrap._load_sandbox_provider", side_effect=sandbox_setup
    ):
        first = _runtime(tmp_path / "user", None)
        second = _runtime(tmp_path / "user", second_dir)
    try:
        assert first.project.dir == str(first_dir.resolve())
        assert second.project.dir == str(second_dir.resolve())
        assert captured[0] == captured[1]
        assert Path.cwd() == first_dir
    finally:
        first.close()
        second.close()


def test_thread_index_updates_rebuild_and_project_isolation(tmp_path):
    root = tmp_path / "user"
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    store = ThreadStore(root)
    store.projects.ensure(a)
    store.projects.ensure(b)
    first = store.create("a1", project="a", cwd=a)
    second = store.create("b1", project="b", cwd=b)
    assert store.list_project("a") == [first]
    assert store.list_project("b") == [second]
    renamed = store.update("a1", title="renamed")
    assert (renamed.project, renamed.cwd) == ("a", str(a.resolve()))
    index_a = root / "sessions" / "a" / "threads.json"
    index_b = root / "sessions" / "b" / "threads.json"
    assert json.loads(index_a.read_text())[0]["title"] == "renamed"
    index_a.write_text("broken")
    assert store.list_project("a") == [renamed]
    assert store.list_project("b") == [second]
    index_a.unlink()
    assert store.list_project("a") == [renamed]
    store.delete("a1")
    assert store.list_project("a") == []
    assert store.list_project("b") == [second]
    old = store.create("old")
    assert old.project is None and old.cwd is None
    assert old in store.list()
    assert old not in store.list_project("b")
    assert json.loads(index_b.read_text())[0]["thread_id"] == "b1"


def test_runtime_switch_restore_and_run_record(tmp_path, monkeypatch):
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    root = tmp_path / "user"
    before = Path.cwd()
    runtime = _runtime(root, a)
    try:
        assert Path.cwd() == before
        first = runtime.thread_id
        assert runtime.thread_store.require(first).cwd == str(a.resolve())
        result = runtime.run_question("first")
        record_path = runtime.thread_dir / "runs" / result.run_id / "record.json"
        record = json.loads(record_path.read_text())
        assert (record["project"], record["cwd"]) == ("a", str(a.resolve()))
        runtime.project_store.ensure(b)
        switched = runtime.switch_project("b")
        assert switched.project.project_name == "b"
        assert switched.thread_id != first
        assert (
            switched.checkpointer.get_tuple(
                {"configurable": {"thread_id": switched.thread_id}}
            )
            is None
        )
        second = switched.run_question("second")
        assert [m.content for m in second.state["messages"] if m.type == "human"] == [
            "second"
        ]
        with pytest.raises(ValueError, match="current project"):
            switched.switch_project_thread(first)
        with pytest.raises(KeyError):
            switched.switch_project("missing")
        assert switched.project.project_name == "b"
        assert switched.thread_id != first
        restored = switched.switch_thread(first)
        assert restored.project.project_name == "a"
        continued = restored.run_question("third")
        assert [
            m.content for m in continued.state["messages"] if m.type == "human"
        ] == ["first", "third"]
        b.rmdir()
        with pytest.raises(FileNotFoundError):
            restored.switch_project("b")
        assert restored.project.project_name == "a"
        assert restored.thread_id == first
        legacy = restored.thread_store.create("legacy")
        legacy_path = (
            restored.thread_store.session_dir(legacy.thread_id) / "metadata.json"
        )
        legacy_payload = json.loads(legacy_path.read_text())
        legacy_payload.pop("project")
        legacy_payload.pop("cwd")
        legacy_path.write_text(json.dumps(legacy_payload))
        unbound = restored.switch_thread(legacy.thread_id)
        assert unbound.project.project_name == "a"
        assert unbound.thread_store.require("legacy").project is None
        from_legacy = unbound.new_thread()
        assert from_legacy.thread_store.require(from_legacy.thread_id).project == "a"
        assert Path.cwd() == before
    finally:
        runtime.close()


def test_project_thread_restore_failure_keeps_current_runtime(tmp_path, monkeypatch):
    directory = tmp_path / "project"
    directory.mkdir()
    runtime = _runtime(tmp_path / "user", directory)
    try:
        target = runtime.thread_store.create(
            "target", project=runtime.project.project_name, cwd=runtime.project.dir
        )
        current_id = runtime.thread_id
        monkeypatch.setattr(
            runtime.checkpointer,
            "get_tuple",
            lambda config: (_ for _ in ()).throw(ValueError("corrupt checkpoint")),
        )
        with pytest.raises(ValueError, match="corrupt checkpoint"):
            runtime.switch_project_thread(target.thread_id)
        assert runtime.thread_id == current_id
        assert runtime.project.project_name == "project"
    finally:
        runtime.close()


@pytest.mark.parametrize("count", [0, 1, 10, 11])
def test_project_selector_noninteractive_and_boundaries(tmp_path, count):
    store = ProjectStore(tmp_path / "user")
    for index in range(count):
        directory = tmp_path / f"p{index}"
        directory.mkdir()
        store.ensure(directory)
    output = StringIO()
    console = Console(file=output, force_terminal=False)
    assert (
        asyncio.run(select_project(store.list(), None, console, interactive=False))
        is None
    )
    assert output.getvalue().count(" — ") == count
    thread_store = ThreadStore(tmp_path / "user")
    bound_project = store.ensure(tmp_path, "root-project")
    for index in range(count):
        thread_store.create(
            f"t{index}", project=bound_project.project_name, cwd=tmp_path
        )
    assert len(thread_store.list_project(bound_project.project_name)) == count
    output = StringIO()
    assert (
        asyncio.run(
            select_thread(
                thread_store.list(), "", Console(file=output), interactive=False
            )
        )
        is None
    )
    assert output.getvalue().count("[t") == count


def test_project_selector_keyboard_and_pagination(tmp_path):
    store = ProjectStore(tmp_path / "user")
    for index in range(11):
        directory = tmp_path / f"p{index}"
        directory.mkdir()
        store.ensure(directory)
    items = store.list()
    console = Console(file=StringIO())

    async def drive(keys):
        with create_pipe_input() as pipe:
            task = asyncio.create_task(
                select_project(
                    items,
                    None,
                    console,
                    input=pipe,
                    output=DummyOutput(),
                    interactive=True,
                )
            )
            await asyncio.sleep(0.05)
            pipe.send_text(keys)
            return await asyncio.wait_for(task, 2)

    assert asyncio.run(drive("\x1b[B" * 10 + "\r")) == items[10].project_name
    assert asyncio.run(drive("\x1b[A" * 2 + "\r")) == items[0].project_name
    assert asyncio.run(drive("\x1b")) is None


def test_tui_project_picker_keyboard():
    async def check():
        results = []
        app = App()
        entries = [(str(i), f"Item {i}") for i in range(11)]
        async with app.run_test() as pilot:
            picker = ProjectPicker(entries, title="Projects")
            app.push_screen(picker, results.append)
            await pilot.pause()
            await pilot.press(*(["down"] * 10), "enter")
            await pilot.pause()
            assert results == ["10"]
            app.push_screen(ProjectPicker(entries, title="Projects"), results.append)
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert results == ["10", None]

    asyncio.run(check())


def test_tui_project_commands_switch_and_restore(tmp_path):
    first_dir = tmp_path / "first"
    first_dir.mkdir()
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    runtime = _runtime(tmp_path / "user", first_dir)
    runtime.project_store.ensure(second_dir)

    async def check():
        app = PoirotTUI(runtime)
        async with app.run_test(size=(120, 40)) as pilot:
            app._handle_command("/project list")
            await pilot.pause()
            picker = app.screen
            assert isinstance(picker, ProjectPicker)
            target = next(
                i for i, (name, _) in enumerate(picker.entries) if name == "second"
            )
            await pilot.press(*(["down"] * target), "enter")
            await pilot.pause()
            assert app.runtime.project.project_name == "second"
            second_thread = app.runtime.thread_id
            app._handle_command("/project_thread list")
            await pilot.pause()
            assert isinstance(app.screen, ProjectPicker)
            await pilot.press("enter")
            await pilot.pause()
            assert app.runtime.thread_id == second_thread

    try:
        asyncio.run(check())
    finally:
        runtime.close()


def test_cli_startup_project_arguments(monkeypatch, tmp_path):
    from poirot.backend.app.cli import main as cli_main

    monkeypatch.setattr(
        "poirot.backend.app.cli.setup_wizard.ensure_config", lambda root: True
    )
    calls = []
    monkeypatch.setattr(
        cli_main, "run_chat", lambda **kwargs: calls.append(kwargs) or 0
    )
    assert cli_main.main(["--dir", str(tmp_path), "--project_name", "named"]) == 0
    assert calls[-1]["project_dir"] == str(tmp_path)
    assert calls[-1]["project_name"] == "named"
    assert cli_main.main([]) == 0
    assert calls[-1]["project_dir"] is None
    assert cli_main.main(["cli", "--dir", str(tmp_path), "--project_name", "named"]) == 0
    assert calls[-1]["legacy"] is True
    assert calls[-1]["project_name"] == "named"


def test_project_command_pending_flags(tmp_path):
    from poirot.backend.app.cli.commands import handle_command

    directory = tmp_path / "project"
    directory.mkdir()
    runtime = _runtime(tmp_path / "user", directory)
    state = {}
    console = Console(file=StringIO())
    try:
        handle_command("/project list", console, None, state, runtime)
        handle_command("/project_thread list", console, None, state, runtime)
        assert state["pending_project_list"] is True
        assert state["pending_project_thread_list"] is True
    finally:
        runtime.close()


_PROCESS = r"""
import json, os, sys
from unittest.mock import patch
from poirot.backend.tests.v1._fake_model import FakeChatModelWithTools
from poirot.backend.agents.config.model_router import ModelRouter
from poirot.backend.app.bootstrap import bootstrap_runtime

with patch.object(ModelRouter, "build_model", lambda self, role: FakeChatModelWithTools(responses=["answer"])), patch.object(ModelRouter, "chain_names", lambda self, role: ["fake"]), patch("poirot.backend.app.bootstrap._check_node_available", return_value=False):
    rt = bootstrap_runtime(cli_overrides={"storage_root": os.environ["ROOT"], "logs_root": os.environ["ROOT"] + "/logs"}, project_dir=os.environ["A"])
try:
    if sys.argv[1] == "create":
        aid = rt.thread_id
        rt.run_question("a-first")
        rt.project_store.ensure(os.environ["B"])
        rt = rt.switch_project("b")
        bid = rt.thread_id
        rt.run_question("b-first")
        print(json.dumps({"a": aid, "b": bid}))
    else:
        ids = json.loads(os.environ["IDS"])
        assert len(rt.project_store.list()) == 2
        assert len(rt.thread_store.list_project("a")) >= 1
        assert len(rt.thread_store.list_project("b")) == 1
        a = rt.switch_project_thread(ids["a"])
        ar = a.run_question("a-second")
        b = a.switch_thread(ids["b"])
        br = b.run_question("b-second")
        human = lambda result: [m.content for m in result.state["messages"] if m.type == "human"]
        print(json.dumps({"a": human(ar), "b": human(br), "a_title": a.thread_store.require(ids["a"]).title,
                          "b_title": b.thread_store.require(ids["b"]).title,
                          "a_project": a.thread_store.require(ids["a"]).project,
                          "b_project": b.thread_store.require(ids["b"]).project}))
finally:
    rt.close()
"""


def test_projects_and_checkpoints_across_processes(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    root = tmp_path / "user"
    env = {
        **os.environ,
        "ROOT": str(root),
        "A": str(a),
        "B": str(b),
        "POIROT_MULTIAGENT_ENABLED": "false",
        "POIROT_SKILL_ENABLED": "false",
        "POIROT_MEMORY_USE": "",
        "POIROT_MCP_ENABLED": "false",
    }

    def run(phase: str, ids: dict | None = None):
        proc = subprocess.run(
            [sys.executable, "-c", _PROCESS, phase],
            check=False,
            cwd=Path(__file__).parents[5],
            env={**env, "IDS": json.dumps(ids or {})},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return json.loads(proc.stdout.strip().splitlines()[-1])

    ids = run("create")
    result = run("resume", ids)
    assert result["a"] == ["a-first", "a-second"]
    assert result["b"] == ["b-first", "b-second"]
    assert result["a_project"] == "a" and result["b_project"] == "b"
    assert "a-first" in result["a_title"] and "b-first" in result["b_title"]
    assert (root / "projects" / "a.json").is_file()
    assert (root / "projects" / "b.json").is_file()
    assert (root / "sessions" / "a" / "threads.json").is_file()
    assert (root / "sessions" / "b" / "threads.json").is_file()
