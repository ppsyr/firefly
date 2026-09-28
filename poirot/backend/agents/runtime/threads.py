"""User-level thread metadata; graph state remains in LangGraph checkpoints."""
from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
import warnings
from dataclasses import dataclass, replace, asdict
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from uuid import uuid4

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def storage_root(root: str | Path | None = None) -> Path:
    value = root if root is not None else os.environ.get("POIROT_STORAGE_ROOT")
    return (Path(value).expanduser() if value else Path.home() / ".poirot").resolve()


def validate_thread_id(thread_id: str) -> str:
    if not isinstance(thread_id, str) or not _ID.fullmatch(thread_id):
        raise ValueError("Invalid thread ID")
    return thread_id


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _prefix(created_at: str) -> str:
    return datetime.fromisoformat(created_at).astimezone().strftime("%Y-%m-%d %H:%M")


def _clean_title(value: str) -> str:
    text = "".join(" " if unicodedata.category(c) in {"Cc", "Cf"} else c for c in value)
    text = " ".join(text.split())
    if not text:
        raise ValueError("Title cannot be empty")
    return text


@dataclass(frozen=True)
class ThreadMetadata:
    thread_id: str
    title: str
    created_at: str
    updated_at: str
    title_set: bool = False


class ThreadStore:
    def __init__(self, root: str | Path | None = None):
        self.storage_root = storage_root(root)
        self.root = self.storage_root / "sessions"
        self._legacy_root = self.storage_root / "threads"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def _path(self, thread_id: str) -> Path:
        thread_id = validate_thread_id(thread_id)
        pattern = r"thread-\d{2}-\d{2}-\d{2}-" + re.escape(thread_id)
        paths = [path for path in self.root.glob(f"*/*/*/thread-*-{thread_id}/metadata.json") if re.fullmatch(pattern, path.parent.name)]
        if len(paths) > 1:
            raise ValueError(f"Duplicate session directories for thread: {thread_id}")
        if paths:
            return paths[0]
        return self._legacy_root / f"{thread_id}.json"

    def _new_path(self, item: ThreadMetadata) -> Path:
        created = datetime.fromisoformat(item.created_at).astimezone()
        return self.root / created.strftime("%Y/%m/%d") / f"thread-{created:%H-%M-%S}-{item.thread_id}" / "metadata.json"

    def session_dir(self, thread_id: str) -> Path:
        with self._lock:
            self.require(thread_id)
            return self._path(thread_id).parent

    def _read(self, path: Path) -> ThreadMetadata:
        data = json.loads(path.read_text(encoding="utf-8"))
        for key in ("thread_id", "title", "created_at", "updated_at"):
            if not isinstance(data.get(key), str) or not data[key]:
                raise ValueError(f"Invalid metadata field: {key}")
        thread_id = validate_thread_id(data["thread_id"])
        if path.name == "metadata.json":
            matches = re.fullmatch(r"thread-\d{2}-\d{2}-\d{2}-" + re.escape(thread_id), path.parent.name)
        else:
            matches = path.stem == thread_id
        if not matches:
            raise ValueError("Thread ID does not match filename")
        for key in ("created_at", "updated_at"):
            if datetime.fromisoformat(data[key]).tzinfo is None:
                raise ValueError(f"Timestamp lacks timezone: {key}")
        if "title_set" in data and not isinstance(data["title_set"], bool):
            raise ValueError("Invalid metadata field: title_set")
        return ThreadMetadata(**{key: data[key] for key in ThreadMetadata.__dataclass_fields__ if key in data})

    def get(self, thread_id: str) -> ThreadMetadata | None:
        with self._lock:
            path = self._path(thread_id)
            if not path.exists():
                return None
            item = self._read(path)
            if path.parent == self._legacy_root:
                self._write(item)
            return item

    def require(self, thread_id: str) -> ThreadMetadata:
        item = self.get(thread_id)
        if item is None:
            raise KeyError(f"Thread not found: {thread_id}")
        return item

    def _write(self, item: ThreadMetadata) -> None:
        path = self._path(item.thread_id)
        if path.parent == self._legacy_root:
            path = self._new_path(item)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".thread-", delete=False) as file:
                temp = Path(file.name)
                json.dump(asdict(item), file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp, path)
            (self._legacy_root / f"{item.thread_id}.json").unlink(missing_ok=True)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)

    def create(self, thread_id: str | None = None) -> ThreadMetadata:
        with self._lock:
            thread_id = validate_thread_id(thread_id or str(uuid4()))
            if self._path(thread_id).exists():
                raise FileExistsError(f"Thread already exists: {thread_id}")
            now = _now()
            item = ThreadMetadata(thread_id, f"{_prefix(now)} 新会话", now, now)
            self._write(item)
            return item

    def update(self, thread_id: str, *, first_message: str | None = None, title: str | None = None) -> ThreadMetadata:
        with self._lock:
            item = self.require(thread_id)
            if title is not None:
                item = replace(item, title=_clean_title(title), title_set=True)
            elif first_message is not None and first_message.strip() and not item.title_set:
                item = replace(item, title=f"{_prefix(item.created_at)} {_clean_title(first_message)[:60]}", title_set=True)
            item = replace(item, updated_at=_now())
            self._write(item)
            return item

    def list(self) -> list[ThreadMetadata]:
        with self._lock:
            items = []
            paths = list(self.root.glob("*/*/*/thread-*/metadata.json")) + list(self._legacy_root.glob("*.json"))
            seen = set()
            for path in paths:
                try:
                    item = self._read(path)
                    if item.thread_id not in seen:
                        items.append(self.require(item.thread_id))
                        seen.add(item.thread_id)
                except Exception as exc:
                    warnings.warn(f"Invalid thread metadata {path}: {exc}", stacklevel=2)
            return sorted(items, key=lambda item: (datetime.fromisoformat(item.updated_at), item.thread_id), reverse=True)

    def delete(self, thread_id: str) -> None:
        with self._lock:
            path = self._path(thread_id)
            if not path.exists():
                raise KeyError(f"Thread not found: {thread_id}")
            path.unlink()
            (self._legacy_root / f"{thread_id}.json").unlink(missing_ok=True)
