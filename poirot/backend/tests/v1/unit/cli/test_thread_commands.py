from __future__ import annotations

from io import StringIO
from types import SimpleNamespace

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
    console = Console(file=output, force_terminal=False)
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
