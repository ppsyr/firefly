"""User-level thread metadata; graph state remains in LangGraph checkpoints."""
from __future__ import annotations

import json
import builtins
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

from poirot.backend.agents.runtime.projects import ProjectStore, normalize_project_dir, validate_project_name

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
    project: str | None = None
    cwd: str | None = None
    extra_dirs: tuple[str, ...] = ()


class ThreadStore:
    def __init__(self, root: str | Path | None = None):
        self.storage_root = storage_root(root)
        self.root = self.storage_root / "sessions"
        self._legacy_root = self.storage_root / "threads"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        # New interactive runtimes reserve an ID before the first user turn,
        # but keep that metadata in memory until the turn is actually sent.
        self._pending: dict[str, ThreadMetadata] = {}
        self.projects = ProjectStore(self.storage_root)

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
            item = self.require(thread_id)
            path = self._path(thread_id)
            return path.parent if path.exists() else self._new_path(item).parent

    def _read(self, path: Path) -> ThreadMetadata:
        data = json.loads(path.read_text(encoding="utf-8"))
        for key in ("thread_id", "title", "created_at", "updated_at"):
            if not isinstance(data.get(key), str) or not data[key]:
                raise ValueError(f"Invalid metadata field: {key}")
        thread_id = validate_thread_id(data["thread_id"])
        if path.name == "metadata.json":
            matches_name = re.fullmatch(r"thread-\d{2}-\d{2}-\d{2}-" + re.escape(thread_id), path.parent.name) is not None
        else:
            matches_name = path.stem == thread_id
        if not matches_name:
            raise ValueError("Thread ID does not match filename")
        for key in ("created_at", "updated_at"):
            if datetime.fromisoformat(data[key]).tzinfo is None:
                raise ValueError(f"Timestamp lacks timezone: {key}")
        if "title_set" in data and not isinstance(data["title_set"], bool):
            raise ValueError("Invalid metadata field: title_set")
        if (data.get("project") is None) != (data.get("cwd") is None):
            raise ValueError("Thread project and cwd must be supplied together")
        if data.get("project") is not None:
            validate_project_name(data["project"])
            if not isinstance(data["cwd"], str) or not Path(data["cwd"]).is_absolute():
                raise ValueError("Invalid metadata field: cwd")
        else:
            data["project"] = None
            data["cwd"] = None
        extra_dirs = data.get("extra_dirs", ())
        if not isinstance(extra_dirs, (list, tuple)) or any(
            not isinstance(value, str) or not Path(value).is_absolute() for value in extra_dirs
        ):
            raise ValueError("Invalid metadata field: extra_dirs")
        data["extra_dirs"] = tuple(str(Path(value).expanduser().resolve(strict=False)) for value in extra_dirs)
        return ThreadMetadata(**{key: data[key] for key in ThreadMetadata.__dataclass_fields__ if key in data})

    def get(self, thread_id: str) -> ThreadMetadata | None:
        with self._lock:
            pending = self._pending.get(thread_id)
            if pending is not None:
                return pending
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
            self._pending.pop(item.thread_id, None)
            self._sync_project(item.project)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)

    def _sync_project(self, project: str | None) -> None:
        if project is not None:
            self.projects.sync_thread_index(project)
            self.projects.touch(project)

    def create(
        self,
        thread_id: str | None = None,
        *,
        project: str | None = None,
        cwd: str | Path | None = None,
        persist: bool = True,
    ) -> ThreadMetadata:
        with self._lock:
            thread_id = validate_thread_id(thread_id or str(uuid4()))
            if self.get(thread_id) is not None:
                raise FileExistsError(f"Thread already exists: {thread_id}")
            now = _now()
            if (project is None) != (cwd is None):
                raise ValueError("Project and cwd must be supplied together")
            if project is not None:
                metadata = self.projects.get(validate_project_name(project))
                canonical = str(normalize_project_dir(cwd))
                if metadata is None or canonical != metadata.dir:
                    raise ValueError("Thread project and cwd do not match a registered project")
                cwd = canonical
            item = ThreadMetadata(thread_id, f"{_prefix(now)} 新会话", now, now, project=project, cwd=str(cwd) if cwd else None)
            if persist:
                self._write(item)
            else:
                self._pending[thread_id] = item
            return item

    def materialize(self, thread_id: str) -> ThreadMetadata:
        """Persist a reserved interactive thread on its first user turn."""
        with self._lock:
            item = self._pending.get(thread_id)
            if item is None:
                return self.require(thread_id)
            self._write(item)
            return self.require(thread_id)

    def is_pending(self, thread_id: str) -> bool:
        with self._lock:
            return thread_id in self._pending

    def update(self, thread_id: str, *, first_message: str | None = None, title: str | None = None) -> ThreadMetadata:
        with self._lock:
            item = self.require(thread_id)
            if title is not None:
                item = replace(item, title=_clean_title(title), title_set=True)
            elif first_message is not None and first_message.strip() and not item.title_set:
                item = replace(item, title=f"{_prefix(item.created_at)} {_clean_title(first_message)[:60]}", title_set=True)
            item = replace(item, updated_at=_now())
            if thread_id in self._pending:
                self._pending[thread_id] = item
            else:
                self._write(item)
            return item

    def add_extra_dir(self, thread_id: str, directory: str | Path) -> ThreadMetadata:
        """Add a canonical read-only reference directory to one thread."""
        with self._lock:
            item = self.require(thread_id)
            canonical = Path(directory).expanduser().resolve(strict=True)
            if not canonical.is_dir():
                raise ValueError(f"Reference directory is not a directory: {directory}")
            if not os.access(canonical, os.R_OK | os.X_OK):
                raise ValueError(f"Reference directory is not accessible: {directory}")
            value = str(canonical)
            if value in item.extra_dirs or (item.cwd and value == str(Path(item.cwd).resolve())):
                raise FileExistsError(f"Reference directory already added: {value}")
            item = replace(item, extra_dirs=(*item.extra_dirs, value), updated_at=_now())
            if thread_id in self._pending:
                self._pending[thread_id] = item
            else:
                self._write(item)
            return item

    def remove_extra_dir(self, thread_id: str, directory: str | Path) -> ThreadMetadata:
        """Remove a reference directory by its canonical real path."""
        with self._lock:
            item = self.require(thread_id)
            canonical = str(Path(directory).expanduser().resolve(strict=False))
            if canonical not in item.extra_dirs:
                raise KeyError(f"Reference directory is not added: {directory}")
            item = replace(
                item,
                extra_dirs=tuple(value for value in item.extra_dirs if value != canonical),
                updated_at=_now(),
            )
            if thread_id in self._pending:
                self._pending[thread_id] = item
            else:
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

    def list_project(self, project: str) -> builtins.list[ThreadMetadata]:
        with self._lock:
            index = self.projects.thread_index(project)
            items = []
            for entry in index:
                try:
                    item = self.require(entry["thread_id"])
                    if item.project == project:
                        items.append(item)
                except Exception:
                    continue
            return sorted(items, key=lambda item: (datetime.fromisoformat(item.updated_at), item.thread_id), reverse=True)

    def delete(self, thread_id: str) -> None:
        with self._lock:
            if thread_id in self._pending:
                self._pending.pop(thread_id, None)
                return
            path = self._path(thread_id)
            if not path.exists():
                raise KeyError(f"Thread not found: {thread_id}")
            item = self._read(path)
            path.unlink()
            (self._legacy_root / f"{thread_id}.json").unlink(missing_ok=True)
            self._sync_project(item.project)
