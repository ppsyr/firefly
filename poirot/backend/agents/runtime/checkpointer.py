"""SQLite checkpoints shared by synchronous and asynchronous graph entry points."""
from __future__ import annotations

import asyncio
import atexit
import os
from pathlib import Path
from threading import RLock, Thread

import aiosqlite
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


class SQLiteCheckpointer(BaseCheckpointSaver):
    """Keep the async SQLite connection on an owned loop across asyncio.run calls."""

    def __init__(self, path: str | Path):
        super().__init__()
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._loop = asyncio.new_event_loop()
        self._thread = Thread(target=self._loop.run_forever, name="poirot-checkpoints", daemon=True)
        self._closed = False
        self._close_lock = RLock()
        self._thread.start()
        try:
            self._call(self._open())
        except BaseException:
            self.close()
            raise

    async def _open(self):
        self._conn = await aiosqlite.connect(str(self.path))
        self._saver = AsyncSqliteSaver(self._conn)
        await self._saver.setup()

    def _submit(self, coro):
        if self._closed:
            coro.close()
            raise RuntimeError("Checkpoint connection is closed")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _call(self, coro):
        return self._submit(coro).result()

    async def _await(self, coro):
        return await asyncio.wrap_future(self._submit(coro))

    def get_tuple(self, config):
        return self._call(self._saver.aget_tuple(config))

    async def aget_tuple(self, config):
        return await self._await(self._saver.aget_tuple(config))

    async def _collect(self, config, kwargs):
        return [item async for item in self._saver.alist(config, **kwargs)]

    def list(self, config, *, filter=None, before=None, limit=None):
        yield from self._call(self._collect(config, dict(filter=filter, before=before, limit=limit)))

    async def alist(self, config, *, filter=None, before=None, limit=None):
        for item in await self._await(self._collect(config, dict(filter=filter, before=before, limit=limit))):
            yield item

    def put(self, config, checkpoint, metadata, new_versions):
        return self._call(self._saver.aput(config, checkpoint, metadata, new_versions))

    async def aput(self, config, checkpoint, metadata, new_versions):
        return await self._await(self._saver.aput(config, checkpoint, metadata, new_versions))

    def put_writes(self, config, writes, task_id, task_path=""):
        return self._call(self._saver.aput_writes(config, writes, task_id, task_path))

    async def aput_writes(self, config, writes, task_id, task_path=""):
        return await self._await(self._saver.aput_writes(config, writes, task_id, task_path))

    def delete_thread(self, thread_id):
        return self._call(self._saver.adelete_thread(thread_id))

    async def adelete_thread(self, thread_id):
        return await self._await(self._saver.adelete_thread(thread_id))

    def get_next_version(self, current, channel):
        return self._saver.get_next_version(current, channel)

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            try:
                if hasattr(self, "_conn"):
                    self._call(self._conn.close())
            finally:
                self._closed = True
                self._loop.call_soon_threadsafe(self._loop.stop)
                self._thread.join()
                self._loop.close()


_cp: SQLiteCheckpointer | None = None
_lock = RLock()


def get_checkpointer() -> SQLiteCheckpointer:
    """Compatibility factory for standalone agents; app runtimes own their saver."""
    global _cp
    with _lock:
        if _cp is None:
            root = Path(os.environ.get("POIROT_STORAGE_ROOT", "~/.poirot")).expanduser()
            _cp = SQLiteCheckpointer(root / "checkpoints.db")
        return _cp


def reset_checkpointer() -> None:
    global _cp
    with _lock:
        if _cp is not None:
            _cp.close()
            _cp = None


atexit.register(reset_checkpointer)
