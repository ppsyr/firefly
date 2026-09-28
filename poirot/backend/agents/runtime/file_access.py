"""Thread-scoped file access and ``@`` reference resolution.

The thread metadata is the authority for file access.  Project names, the
process working directory, and project indexes are deliberately not consulted
here.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Awaitable, Callable, Iterable


MAX_REFERENCE_FILE_BYTES = 100 * 1024
MAX_DIRECTORY_ENTRIES = 1000
MAX_DIRECTORY_BYTES = 512 * 1024
MAX_DIRECTORY_VISITED = 10_000

# Dependency/build trees contain many thousands of files and are rarely useful
# as interactive ``@`` candidates. These names are skipped while discovering
# candidates; an explicitly supplied path is still checked and resolved by the
# normal thread boundary rules.
IGNORED_SEARCH_DIR_NAMES = frozenset({
    ".git",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "build",
    "dist",
})


class FileAccessError(ValueError):
    """Base class for user-facing file access failures."""


class UnboundThreadError(FileAccessError):
    """The current thread has no persisted cwd binding."""


class FileOutsideThreadError(FileAccessError):
    """A path is outside the current thread's allowed roots."""


class FileReferenceAmbiguous(FileAccessError):
    """A filename reference matched more than one allowed file."""

    def __init__(self, reference: str, candidates: Iterable[str]) -> None:
        self.reference = reference
        self.candidates = tuple(candidates)
        super().__init__(
            f"@{reference} matches multiple files: " + ", ".join(self.candidates)
        )


class FileReferenceNotFound(FileAccessError):
    """An @ reference did not resolve to an allowed file or directory."""


@dataclass(frozen=True)
class FileReference:
    """A single resolved @ reference and its injected material."""

    reference: str
    relative_path: str
    kind: str
    content: str


@dataclass(frozen=True)
class PreparedQuestion:
    """Original question plus one-turn reference material."""

    original: str
    enriched: str
    references: tuple[FileReference, ...] = ()
    thread_references: tuple[Any, ...] = ()


def _relative_display(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix() or "."
    except ValueError:
        return path.as_posix()


class ThreadFileAccess:
    """Resolve and read files under a thread's cwd and optional extra roots."""

    def __init__(self, cwd: str | Path | None, extra_roots: Iterable[str | Path] = ()) -> None:
        if not cwd:
            raise UnboundThreadError(
                "This thread is not bound to a directory; create or switch to a project thread before accessing files."
            )
        self.cwd = Path(cwd).expanduser().resolve()
        if not self.cwd.is_dir():
            raise FileAccessError("The thread's bound directory is unavailable")
        roots = [self.cwd]
        for root in extra_roots:
            resolved = Path(root).expanduser().resolve()
            if resolved.is_dir() and resolved not in roots:
                roots.append(resolved)
        self.roots = tuple(roots)
        # Suggestions are requested once per keystroke. Keep the expensive
        # directory walk on this access object and filter the result in memory.
        self._suggestion_index: tuple[Path, ...] | None = None
        self._suggestion_index_lock = Lock()

    def _under_root(self, path: Path) -> bool:
        return any(_is_relative_to(path, root) for root in self.roots)

    def resolve(self, value: str | Path, *, allow_missing: bool = False) -> Path:
        raw = str(value).strip()
        if not raw:
            raise FileReferenceNotFound("Empty file path")
        candidate = Path(raw).expanduser()
        # Relative references are always relative to the persisted thread cwd.
        if not candidate.is_absolute():
            candidate = self.cwd / candidate
        resolved = candidate.resolve(strict=False)
        if not self._under_root(resolved):
            raise FileOutsideThreadError(f"Path is outside the current thread directory: {raw}")
        if not allow_missing and not resolved.exists():
            raise FileReferenceNotFound(f"File or directory not found: {raw}")
        return resolved

    def read_text(self, value: str | Path) -> tuple[Path, str]:
        path = self.resolve(value)
        if not path.is_file():
            raise FileReferenceNotFound(f"Not a regular file: {value}")
        try:
            with path.open("rb") as handle:
                data = handle.read(MAX_REFERENCE_FILE_BYTES + 1)
        except OSError as exc:
            raise FileAccessError(f"Unable to read {value}") from exc
        if len(data) > MAX_REFERENCE_FILE_BYTES:
            raise FileAccessError(f"File exceeds the 100 KiB reference limit: {value}")
        if b"\x00" in data:
            raise FileAccessError(f"Binary files cannot be referenced: {value}")
        try:
            return path, data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FileAccessError(f"File is not valid UTF-8: {value}") from exc

    def list_text_files(self, value: str | Path) -> tuple[Path, list[tuple[str, int]], bool]:
        root = self.resolve(value)
        if not root.is_dir():
            raise FileReferenceNotFound(f"Not a directory: {value}")
        rows: list[tuple[str, int]] = []
        visited: set[Path] = set()
        truncated = False
        stack = [root]
        while stack:
            current = stack.pop()
            real = current.resolve(strict=False)
            if real in visited:
                continue
            visited.add(real)
            if len(visited) > MAX_DIRECTORY_VISITED:
                truncated = True
                break
            try:
                entries = sorted(current.iterdir(), key=lambda p: p.name)
            except OSError:
                truncated = True
                continue
            for entry in entries:
                if entry.name in IGNORED_SEARCH_DIR_NAMES:
                    continue
                resolved = entry.resolve(strict=False)
                if not self._under_root(resolved):
                    continue
                if entry.is_dir():
                    stack.append(entry)
                    continue
                if not entry.is_file():
                    continue
                try:
                    size = entry.stat().st_size
                except OSError:
                    continue
                # Directory references list text candidates, but do not read bodies.
                try:
                    with entry.open("rb") as handle:
                        sample = handle.read(4100)
                    if b"\x00" in sample:
                        continue
                    sample.decode("utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                if sum(item[1] for item in rows) + size > MAX_DIRECTORY_BYTES:
                    truncated = True
                    return root, sorted(rows), truncated
                rows.append((_relative_display(resolved, self.cwd), size))
                if len(rows) >= MAX_DIRECTORY_ENTRIES:
                    truncated = True
                    return root, sorted(rows), truncated
        return root, sorted(rows), truncated

    def search_filename(self, name: str) -> list[Path]:
        """Search only allowed roots for an exact basename, deterministically."""
        target = Path(name)
        if target.name != name or not name or name in {".", ".."}:
            return []
        self._ensure_suggestion_index()
        return [
            path for path in self._suggestion_index
            if path.name == name
        ]

    def suggest_paths(self, fragment: str, *, limit: int = 100) -> list[Path]:
        """Return allowed files/directories matching an in-progress ``@`` token."""
        fragment = str(fragment).strip().replace("\\", "/")
        if not fragment or fragment in {".", ".."} or fragment.startswith("/"):
            return []
        found: set[Path] = set()
        if "/" in fragment:
            parent_text, _, leaf = fragment.rpartition("/")
            if any(part in IGNORED_SEARCH_DIR_NAMES for part in Path(parent_text).parts):
                return []
            try:
                parent = self.resolve(parent_text or ".")
            except FileAccessError:
                return []
            if not parent.is_dir():
                return []
            try:
                entries = sorted(os.scandir(parent), key=lambda entry: entry.name)
            except OSError:
                return []
            for entry in entries:
                if entry.name in IGNORED_SEARCH_DIR_NAMES:
                    continue
                if not entry.name.startswith(leaf):
                    continue
                resolved = Path(entry.path).resolve(strict=False)
                if not self._under_root(resolved):
                    continue
                if resolved.is_file() or resolved.is_dir():
                    found.add(resolved)
        else:
            self._ensure_suggestion_index()
            found = {
                path for path in self._suggestion_index
                if path.name.startswith(fragment)
            }
        return sorted(found, key=lambda p: _relative_display(p, self.cwd))[:limit]

    def _build_suggestion_index(self) -> tuple[Path, ...]:
        """Walk allowed roots once and retain only safe files/directories."""
        found: set[Path] = set()
        stack = list(self.roots)
        visited: set[Path] = set()
        while stack and len(visited) < MAX_DIRECTORY_VISITED:
            current = stack.pop()
            real = current.resolve(strict=False)
            if real in visited or not self._under_root(real):
                continue
            visited.add(real)
            try:
                entries = os.scandir(real)
            except OSError:
                continue
            with entries:
                for entry in entries:
                    if entry.name in IGNORED_SEARCH_DIR_NAMES:
                        continue
                    resolved = Path(entry.path).resolve(strict=False)
                    if not self._under_root(resolved):
                        continue
                    try:
                        is_dir = resolved.is_dir()
                        is_file = resolved.is_file()
                    except OSError:
                        continue
                    if not (is_file or is_dir):
                        continue
                    found.add(resolved)
                    if is_dir:
                        stack.append(resolved)
        return tuple(sorted(found, key=lambda p: _relative_display(p, self.cwd)))

    def _ensure_suggestion_index(self) -> None:
        if self._suggestion_index is not None:
            return
        # Several canceled TUI workers can briefly overlap. Serialize the
        # first walk so they do not all rescan a large project concurrently.
        with self._suggestion_index_lock:
            if self._suggestion_index is None:
                self._suggestion_index = self._build_suggestion_index()

    def invalidate_suggestion_index(self) -> None:
        """Drop the lazy index after a thread switch or an external refresh."""
        with self._suggestion_index_lock:
            self._suggestion_index = None


_REFERENCE_RE = re.compile(r"(?<![\w@])@([^\s@()]*)")


def reference_fragment_at_cursor(text: str, cursor: int | None = None) -> tuple[str, int] | None:
    """Return ``(fragment, start_offset)`` for the in-progress ``@`` token."""
    cursor = len(text) if cursor is None else max(0, min(cursor, len(text)))
    before = text[:cursor]
    match = re.search(r"(?<![\w@])@([^\s@()]*)$", before)
    if not match:
        return None
    fragment = match.group(1).rstrip(",.;:!?)]}")
    return fragment, match.start()


def extract_references(question: str) -> list[str]:
    """Extract path-like @ tokens while ignoring email addresses and @@."""
    references: list[str] = []
    for match in _REFERENCE_RE.finditer(question):
        if not match.group(1):
            continue
        # Python/TypeScript decorators such as ``@router.get(...)`` are code,
        # not file references.  A token followed by ``(`` is unambiguous.
        if match.end() < len(question) and question[match.end()] == "(":
            continue
        references.append(match.group(1).rstrip(",.;:!?)]}"))
    return references


def prepare_question(
    question: str,
    access: ThreadFileAccess,
    *,
    choose: Callable[[str, list[Path]], Path | None] | None = None,
) -> PreparedQuestion:
    """Resolve @ references and append bounded, one-turn material."""
    refs: list[FileReference] = []
    for reference in extract_references(question):
        path_value = Path(reference)
        if path_value.is_absolute() or "/" in reference:
            path = access.resolve(reference)
        else:
            matches = access.search_filename(reference)
            if not matches:
                # Bare @words are common in prose and source snippets.  Treat
                # them as references only when they look like a path (have an
                # extension) or actually exist as a basename in the thread.
                if "." not in reference:
                    continue
                raise FileReferenceNotFound(f"No allowed file matches @{reference}")
            if len(matches) > 1:
                if choose is None:
                    raise FileReferenceAmbiguous(reference, (_relative_display(p, access.cwd) for p in matches))
                selected = choose(reference, matches)
                if selected is None:
                    raise FileAccessError(f"Reference cancelled: @{reference}")
                path = access.resolve(selected)
            else:
                path = matches[0]
        if path.is_dir():
            _, rows, truncated = access.list_text_files(path)
            lines = [f"{relative} ({size} bytes)" for relative, size in rows]
            if truncated:
                lines.append("[directory listing truncated by safety limits]")
            content = "Directory listing:\n" + ("\n".join(lines) if lines else "(no text files)")
            kind = "directory"
        else:
            _, text = access.read_text(path)
            content = text
            kind = "file"
        refs.append(FileReference(reference, _relative_display(path, access.cwd), kind, content))
    if not refs:
        return PreparedQuestion(question, question)
    blocks = []
    for item in refs:
        blocks.append(f"[Referenced {item.kind}: {item.relative_path}]\n{item.content}\n[End referenced {item.kind}]")
    return PreparedQuestion(question, question + "\n\n" + "\n\n".join(blocks), tuple(refs))


async def prepare_question_async(
    question: str,
    access: ThreadFileAccess,
    *,
    choose: Callable[[str, list[Path]], Awaitable[Path | None]] | None = None,
) -> PreparedQuestion:
    """Async counterpart used by CLI/TUI selectors for ambiguous basenames."""
    selected: dict[str, Path] = {}
    if choose is not None:
        for reference in extract_references(question):
            if Path(reference).is_absolute() or "/" in reference:
                continue
            matches = access.search_filename(reference)
            if len(matches) > 1:
                path = await choose(reference, matches)
                if path is None:
                    raise FileAccessError(f"Reference cancelled: @{reference}")
                selected[reference] = path
    chooser = (lambda reference, _matches: selected[reference]) if selected else None
    return prepare_question(question, access, choose=chooser)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
