"""SQLite checkpoints shared by synchronous and asynchronous graph entry points."""
from __future__ import annotations

import asyncio
import atexit
import sqlite3
from pathlib import Path
from threading import RLock, Thread

import aiosqlite
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from poirot.backend.agents.runtime.threads import ThreadStore


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


class SessionCheckpointer(BaseCheckpointSaver):
    """Route each thread to the checkpoint database in its session directory."""

    def __init__(self, store: ThreadStore):
        super().__init__()
        self.store = store
        self._savers: dict[str, SQLiteCheckpointer] = {}
        self._lock = RLock()
        self._closed = False

    def _saver(self, config) -> SQLiteCheckpointer:
        thread_id = config["configurable"]["thread_id"]
        with self._lock:
            if self._closed:
                raise RuntimeError("Checkpoint connection is closed")
            if thread_id not in self._savers:
                if self.store.get(thread_id) is None:
                    self.store.create(thread_id)
                directory = self.store.session_dir(thread_id)
                path = directory / "checkpoints.db"
                saver = SQLiteCheckpointer(path)
                try:
                    self._migrate_legacy(thread_id, saver)
                except BaseException:
                    saver.close()
                    raise
                self._savers[thread_id] = saver
            return self._savers[thread_id]

    def _migrate_legacy(self, thread_id: str, saver: SQLiteCheckpointer) -> None:
        legacy = self.store.storage_root / "checkpoints.db"
        if not legacy.exists():
            return
        with sqlite3.connect(legacy) as source, sqlite3.connect(saver.path) as destination:
            exists = destination.execute("SELECT 1 FROM checkpoints WHERE thread_id = ? LIMIT 1", (thread_id,)).fetchone()
            if exists:
                return
            for table in ("checkpoints", "writes"):
                try:
                    rows = source.execute(f"SELECT * FROM {table} WHERE thread_id = ?", (thread_id,)).fetchall()
                except sqlite3.OperationalError as exc:
                    if "no such table" in str(exc):
                        continue
                    raise
                if rows:
                    placeholders = ", ".join("?" for _ in rows[0])
                    destination.executemany(f"INSERT OR IGNORE INTO {table} VALUES ({placeholders})", rows)

    def get_tuple(self, config):
        return self._saver(config).get_tuple(config)

    async def aget_tuple(self, config):
        return await self._saver(config).aget_tuple(config)

    def list(self, config, *, filter=None, before=None, limit=None):
        if config is not None:
            yield from self._saver(config).list(config, filter=filter, before=before, limit=limit)
            return
        items = []
        for thread in self.store.list():
            thread_config = {"configurable": {"thread_id": thread.thread_id}}
            items.extend(self._saver(thread_config).list(thread_config, filter=filter, before=before, limit=limit))
        items.sort(key=lambda item: item.config["configurable"]["checkpoint_id"], reverse=True)
        yield from items if limit is None else items[:limit]

    async def alist(self, config, *, filter=None, before=None, limit=None):
        if config is None:
            for item in await asyncio.to_thread(lambda: list(self.list(config, filter=filter, before=before, limit=limit))):
                yield item
            return
        async for item in self._saver(config).alist(config, filter=filter, before=before, limit=limit):
            yield item

    def put(self, config, checkpoint, metadata, new_versions):
        return self._saver(config).put(config, checkpoint, metadata, new_versions)

    async def aput(self, config, checkpoint, metadata, new_versions):
        return await self._saver(config).aput(config, checkpoint, metadata, new_versions)

    def put_writes(self, config, writes, task_id, task_path=""):
        return self._saver(config).put_writes(config, writes, task_id, task_path)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        return await self._saver(config).aput_writes(config, writes, task_id, task_path)

    def delete_thread(self, thread_id):
        with self._lock:
            saver = self._saver({"configurable": {"thread_id": thread_id}})
            saver.delete_thread(thread_id)
            legacy = self.store.storage_root / "checkpoints.db"
            if legacy.exists():
                with sqlite3.connect(legacy) as conn:
                    for table in ("checkpoints", "writes"):
                        conn.execute(f"DELETE FROM {table} WHERE thread_id = ?", (thread_id,))
            saver.close()
            self._savers.pop(thread_id)

    async def adelete_thread(self, thread_id):
        return await asyncio.to_thread(self.delete_thread, thread_id)

    def get_next_version(self, current, channel):
        return AsyncSqliteSaver.get_next_version(None, current, channel)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for saver in self._savers.values():
                saver.close()
            self._savers.clear()


_cp: SessionCheckpointer | None = None
_lock = RLock()


def get_checkpointer() -> SessionCheckpointer:
    """Compatibility factory for standalone agents; app runtimes own their saver."""
    global _cp
    with _lock:
        if _cp is None:
            _cp = SessionCheckpointer(ThreadStore())
        return _cp


def reset_checkpointer() -> None:
    global _cp
    with _lock:
        if _cp is not None:
            _cp.close()
            _cp = None


atexit.register(reset_checkpointer)
