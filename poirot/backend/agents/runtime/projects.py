"""Persistent project metadata and project-to-thread indexes."""

from __future__ import annotations

import builtins
import json
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock

_PROJECT_NAME = re.compile(r"^[^/\\]+$")


def normalize_project_dir(
    value: str | Path | None = None, *, base: Path | None = None
) -> Path:
    """Resolve a project directory without changing the process cwd."""
    path = Path(value if value is not None else Path.cwd()).expanduser()
    if not path.is_absolute():
        path = (base or Path.cwd()) / path
    path = path.resolve(strict=True)
    if not path.is_dir():
        raise ValueError(f"Project directory is not a directory: {path}")
    return path


def validate_project_name(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "." in value
        or not _PROJECT_NAME.fullmatch(value)
    ):
        raise ValueError(
            "Invalid project name: names cannot contain path separators, '.' or '..'"
        )
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class ProjectMetadata:
    project_name: str
    dir: str
    created_at: str
    updated_at: str


class ProjectStore:
    """Store project metadata and rebuildable thread indexes under storage_root."""

    def __init__(self, root: str | Path | None = None):
        from poirot.backend.agents.runtime.threads import storage_root

        self.storage_root = storage_root(root)
        self.projects_root = self.storage_root / "projects"
        self.sessions_root = self.storage_root / "sessions"
        self.projects_root.mkdir(parents=True, exist_ok=True)
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def _path(self, name: str) -> Path:
        return self.projects_root / f"{validate_project_name(name)}.json"

    @staticmethod
    def _atomic_write(path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent, prefix=".project-", delete=False
            ) as handle:
                temp = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)

    def _read(self, path: Path) -> ProjectMetadata:
        data = json.loads(path.read_text(encoding="utf-8"))
        name = validate_project_name(data.get("project_name"))
        if not isinstance(data.get("dir"), str) or not Path(data["dir"]).is_absolute():
            raise ValueError("Invalid project metadata field: dir")
        directory = Path(data["dir"]).resolve()
        if path.name != f"{name}.json":
            raise ValueError("Project name does not match filename")
        for key in ("created_at", "updated_at"):
            if (
                not isinstance(data.get(key), str)
                or datetime.fromisoformat(data[key]).tzinfo is None
            ):
                raise ValueError(f"Invalid project metadata field: {key}")
        return ProjectMetadata(
            name, str(directory), data["created_at"], data["updated_at"]
        )

    def get(self, name: str) -> ProjectMetadata | None:
        with self._lock:
            path = self._path(name)
            if not path.exists():
                return None
            return self._read(path)

    def list(self) -> list[ProjectMetadata]:
        with self._lock:
            items: list[ProjectMetadata] = []
            for path in self.projects_root.glob("*.json"):
                try:
                    items.append(self._read(path))
                except (ValueError, OSError, KeyError, TypeError, AttributeError):
                    continue
            return sorted(
                items,
                key=lambda item: (
                    datetime.fromisoformat(item.updated_at),
                    item.project_name,
                ),
                reverse=True,
            )

    def ensure(
        self,
        directory: str | Path | None = None,
        project_name: str | None = None,
        *,
        base: Path | None = None,
    ) -> ProjectMetadata:
        """Create or reuse the unique project for a canonical directory."""
        directory_path = normalize_project_dir(directory, base=base)
        requested_name = (
            validate_project_name(project_name)
            if project_name is not None
            else directory_path.name
        )
        with self._lock:
            metadata = self.list()
            by_name = {item.project_name: item for item in metadata}
            same_directory = next(
                (item for item in metadata if Path(item.dir) == directory_path), None
            )
            if same_directory is not None:
                if (
                    project_name is not None
                    and same_directory.project_name != requested_name
                ):
                    raise ValueError(
                        f"Directory is already bound to project '{same_directory.project_name}'"
                    )
                requested_name = same_directory.project_name
            existing = by_name.get(requested_name)
            if existing is not None:
                if Path(existing.dir) != directory_path:
                    raise ValueError(
                        f"Project name '{requested_name}' is already bound to {existing.dir}"
                    )
                return existing
            if self._path(requested_name).exists():
                raise ValueError(f"Project metadata is invalid: {requested_name}")
            now = _now()
            item = ProjectMetadata(requested_name, str(directory_path), now, now)
            self._atomic_write(self._path(requested_name), asdict(item))
            return item

    def touch(self, project_name: str) -> None:
        with self._lock:
            item = self.get(project_name)
            if item is None:
                return
            updated = ProjectMetadata(
                item.project_name, item.dir, item.created_at, _now()
            )
            self._atomic_write(self._path(project_name), asdict(updated))

    def _index_path(self, project_name: str) -> Path:
        return self.sessions_root / validate_project_name(project_name) / "threads.json"

    def rebuild_thread_index(
        self, project_name: str, records: Iterable[Mapping] | None = None
    ) -> builtins.list[dict]:
        """Rebuild the derived index from authoritative thread metadata."""
        validate_project_name(project_name)
        if records is None:
            records = self._scan_thread_metadata()
        entries = []
        for data in records:
            if data.get("project") != project_name or not data.get("cwd"):
                continue
            try:
                directory = Path(str(data["cwd"]))
                if not directory.is_absolute():
                    continue
                updated_at = str(data["updated_at"])
                datetime.fromisoformat(updated_at)
                entries.append(
                    {
                        "thread_id": str(data["thread_id"]),
                        "title": str(data["title"]),
                        "cwd": str(directory),
                        "updated_at": updated_at,
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
        entries.sort(
            key=lambda item: (
                datetime.fromisoformat(item["updated_at"]),
                item["thread_id"],
            ),
            reverse=True,
        )
        self._atomic_write(self._index_path(project_name), entries)
        return entries

    def _scan_thread_metadata(self) -> builtins.list[dict]:
        records: builtins.list[dict] = []
        for path in self.sessions_root.glob("*/*/*/thread-*/metadata.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    records.append(data)
            except (OSError, json.JSONDecodeError):
                continue
        return records

    def thread_index(self, project_name: str) -> builtins.list[dict]:
        """Reconcile the cache with authoritative metadata on every read."""
        with self._lock:
            return self.rebuild_thread_index(project_name)

    def sync_thread_index(self, project_name: str) -> None:
        if self.get(project_name) is not None:
            self.rebuild_thread_index(project_name)
