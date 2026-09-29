from __future__ import annotations

from io import StringIO
from types import SimpleNamespace
from pathlib import Path

from prompt_toolkit.document import Document
from rich.console import Console

from poirot.backend.agents.runtime.threads import ThreadStore
from poirot.backend.app.cli.command_completer import SlashCommandCompleter
from poirot.backend.app.cli.commands import get_registry, handle_command


def test_thread_commands(tmp_path):
    store = ThreadStore(tmp_path)
    current = store.create("first")
    other = store.create("second")
    deleted = []
    runtime = SimpleNamespace(thread_id=current.thread_id, thread_store=store, delete_thread=lambda tid: deleted.append(tid), rename_thread=lambda title: store.update(current.thread_id, title=title))
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=240)
    state = {}

    def command(value):
        output.seek(0)
        output.truncate()
        handle_command(value, console, None, state, runtime)
        return output.getvalue()

    assert "first" in command("/thread")
    assert "Created:" in command("/thread info")
    assert current == store.require("first")
    command("/thread list")
    assert state.pop("pending_thread_list") is True
    command("/thread new")
    assert state.pop("pending_thread_new") is True
    command("/thread switch second")
    assert state.pop("pending_thread_switch") == "second"
    assert "not found" in command("/thread switch absent").lower()
    assert "Invalid thread ID" in command("/thread switch ../bad")
    assert "Usage:" in command("/thread switch")
    assert "Usage:" in command("/thread info extra")
    assert "Unknown" in command("/thread nope")
    assert "Renamed" in command("/thread rename A title with spaces")
    assert store.require("first").title == "A title with spaces"
    assert "empty" in command("/thread rename   ").lower() or "Usage:" in output.getvalue()
    command("/thread delete second")
    assert deleted == [other.thread_id]
    state["_running"] = True
    assert "running" in command("/thread new")
    assert "running" in command("/thread rename blocked")
    assert "running" in command("/thread switch second")


def test_thread_subcommands_complete():
    completer = SlashCommandCompleter(get_registry())
    def candidates(text):
        return {item.text for item in completer.get_completions(Document(text), None)}
    assert candidates("/thread ") == {"info", "list", "new", "switch", "rename", "delete"}
    assert candidates("/thread sw") == {"switch"}


def test_add_dir_command_persists_lists_and_deduplicates(tmp_path: Path):
    project = tmp_path / "project"
    extra = tmp_path / "shared lib"
    project.mkdir()
    extra.mkdir()
    store = ThreadStore(tmp_path / "storage")
    current = store.create("current")
    runtime = SimpleNamespace(
        thread_id=current.thread_id,
        thread_store=store,
        add_reference_dir=lambda value: store.add_extra_dir(current.thread_id, value),
        remove_reference_dir=lambda value: store.remove_extra_dir(current.thread_id, value),
    )
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=240)

    handle_command('/add-dir "{}"'.format(extra), console, None, {}, runtime)
    assert store.require("current").extra_dirs == (str(extra.resolve()),)
    output.seek(0)
    output.truncate()
    handle_command('/add-dir "{}"'.format(extra), console, None, {}, runtime)
    assert "already added" in output.getvalue()
    output.seek(0)
    output.truncate()
    handle_command('/add-dir', console, None, {}, runtime)
    assert str(extra.resolve()) in output.getvalue()
    output.seek(0)
    output.truncate()
    handle_command('/add-dir --remove "{}"'.format(extra), console, None, {}, runtime)
    assert store.require("current").extra_dirs == ()


def test_cd_command_queues_one_path_and_validates_arguments(tmp_path: Path):
    store = ThreadStore(tmp_path / "storage")
    current = store.create("current")
    runtime = SimpleNamespace(thread_id=current.thread_id, thread_store=store)
    output = StringIO()
    console = Console(file=output, force_terminal=False)
    state = {}
    handle_command('/cd "directory with spaces"', console, None, state, runtime)
    assert state["pending_cd"] == "directory with spaces"
    state.clear()
    handle_command('/cd one two', console, None, state, runtime)
    assert "Usage:" in output.getvalue()
    assert "pending_cd" not in state
