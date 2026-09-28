from __future__ import annotations

import asyncio
from io import StringIO

from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from poirot.backend.agents.runtime.threads import ThreadStore
from poirot.backend.app.cli.thread_selector import select_thread


def test_scroll_selects_eleventh_and_cancel(tmp_path):
    store = ThreadStore(tmp_path)
    items = [store.create(f"thread-{i}") for i in range(12)]
    console = Console(file=StringIO())

    async def drive(keys):
        with create_pipe_input() as pipe:
            task = asyncio.create_task(select_thread(items, items[0].thread_id, console, input=pipe, output=DummyOutput(), interactive=True))
            await asyncio.sleep(0.05)
            pipe.send_text(keys)
            return await asyncio.wait_for(task, 2)

    assert asyncio.run(drive("\x1b[B" * 10 + "\r")) == items[10].thread_id
    assert asyncio.run(drive("\x1b[A" * 3 + "\r")) == items[0].thread_id
    assert asyncio.run(drive("\x1b[B" * 30 + "\r")) == items[-1].thread_id
    assert asyncio.run(drive("\x1b")) is None


def test_single_item_and_empty_list(tmp_path):
    store = ThreadStore(tmp_path)
    item = store.create("only")
    console = Console(file=StringIO())
    async def one():
        with create_pipe_input() as pipe:
            task = asyncio.create_task(select_thread([item], item.thread_id, console, input=pipe, output=DummyOutput(), interactive=True))
            await asyncio.sleep(0.05)
            pipe.send_text("\x1b[B\r")
            return await asyncio.wait_for(task, 2)
    assert asyncio.run(one()) == item.thread_id
    assert asyncio.run(select_thread([], item.thread_id, console, interactive=False)) is None


def test_noninteractive_prints_full_ids(tmp_path):
    store = ThreadStore(tmp_path)
    item = store.create("some-full-thread-id")
    output = StringIO()
    result = asyncio.run(select_thread([item], item.thread_id, Console(file=output), interactive=False))
    assert result is None
    assert item.thread_id in output.getvalue()
    assert "switch" in output.getvalue()
