"""Rebuildable FTS5 index for persisted manual reports (schema version 1)."""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import yaml


_FIELDS = {"name", "description", "thread_id", "project", "cwd", "report_created_at", "thread_location", "conversation_location", "schema_version"}
_HAN = re.compile(r"[\u3400-\u9fff]+")
_TERM = re.compile(r"[\u3400-\u9fff]+|[\w./:-]+", re.UNICODE)
_TURN = re.compile(r"\bTurn\s+(\d+)\b", re.IGNORECASE)
_IDENTIFIER = re.compile(r"[A-Za-z0-9_]+(?:[./:-][A-Za-z0-9_]+)+")


@dataclass(frozen=True)
class ReportBlock:
    report_id: str
    report_path: str
    thread_id: str
    project: str | None
    cwd: str | None
    section: str
    level: str
    turn_start: int | None
    turn_end: int | None
    content: str
    content_hash: str
    created_at: str
    score: float = 0.0  # SQLite bm25 rank, not an answer confidence.


@dataclass(frozen=True)
class RebuildResult:
    indexed: int
    skipped: tuple[tuple[str, str], ...]


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cwd(value: str | None) -> str | None:
    return str(Path(value).expanduser().resolve()) if value else None


def _search_text(content: str) -> str:
    extras: list[str] = []
    for match in _HAN.finditer(content):
        word = unicodedata.normalize("NFC", match.group()).casefold()
        extras.extend(word[i:i + 2] for i in range(len(word) - 1))
    for match in _IDENTIFIER.finditer(content):
        extras.append("id" + match.group().casefold().encode("utf-8").hex())
    return content + "\n" + " ".join(extras)


def _query_terms(query: str) -> tuple[str, list[str]]:
    terms: list[str] = []
    exact: list[str] = []
    for match in _TERM.finditer(unicodedata.normalize("NFC", query)):
        token = match.group()
        if _HAN.fullmatch(token):
            if len(token) == 1:
                terms.append(token)
            else:
                terms.extend(token[i:i + 2] for i in range(len(token) - 1))
            exact.append(token.casefold())
        elif _IDENTIFIER.fullmatch(token):
            terms.append("id" + token.casefold().encode("utf-8").hex())
            exact.append(token.casefold())
        else:
            terms.append(token.casefold())
    return " AND ".join('"' + term.replace('"', '""') + '"' for term in terms), exact


def _frontmatter(text: str) -> tuple[dict[str, str], list[str]]:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("missing report frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("unterminated report frontmatter") from exc
    data = yaml.safe_load("\n".join(lines[1:end]))
    if not isinstance(data, dict) or not _FIELDS.issubset(data):
        raise ValueError("missing required report metadata")
    if str(data["schema_version"]) != "1":
        raise ValueError(f"unsupported schema_version: {data['schema_version']}")
    if not all(isinstance(data[key], str) and data[key].strip() for key in ("name", "description", "thread_id", "report_created_at")):
        raise ValueError("invalid required report metadata")
    if not all(data[key] is None or isinstance(data[key], str) for key in _FIELDS):
        raise ValueError("invalid report metadata type")
    return data, lines[end + 1:]


def _parse(text: str, report_path: str) -> tuple[dict[str, str], list[ReportBlock]]:
    meta, lines = _frontmatter(text)
    report_id = _hash(report_path)
    blocks: list[ReportBlock] = []
    section = ""
    level = ""
    body: list[str] = []
    fence: str | None = None
    rag: list[str] = []
    rag_count = 0

    def add(content: str, name: str, layer: str) -> None:
        content = content.strip()
        if not content:
            return
        turns = [int(value) for value in _TURN.findall(content)] if name == "Conversation rounds" else []
        blocks.append(ReportBlock(report_id, report_path, meta["thread_id"], meta.get("project") or None,
                                  _cwd(meta.get("cwd")), name, layer, min(turns) if turns else None,
                                  max(turns) if turns else None, content, _hash(content), meta["report_created_at"]))

    def flush() -> None:
        if body and level in {"L0", "L1"}:
            add("\n".join(body), section, level)
        body.clear()

    add(meta["name"], "Name", "L0")
    add(meta["description"], "Description", "L0")
    for line in lines:
        stripped = line.strip()
        if fence is not None:
            if stripped == "```":
                if fence == "```rag":
                    rag_count += 1
                    add("\n".join(rag), f"Knowledge {rag_count}", "L2")
                rag = []
                fence = None
            elif fence == "```rag":
                rag.append(line)
            continue
        if stripped.startswith("```"):
            fence = "```rag" if stripped == "```rag" and level == "L2" else "```"
            continue
        if stripped.startswith("## "):
            flush()
            heading = stripped[3:]
            if heading == "L0 Overview":
                level, section = "L0", "Overview"
            elif heading == "L1 Structured Summary":
                level, section = "L1", "Structured Summary"
            elif heading == "L2 Knowledge":
                level, section = "L2", "Knowledge"
            else:
                level, section = "", ""
            continue
        if stripped.startswith("### "):
            flush()
            section = stripped[4:] if level == "L1" else section
            continue
        if level in {"L0", "L1"} and stripped and not stripped.startswith("# "):
            if section == "Conversation rounds" and body:
                flush()
            body.append(line)
    if fence is not None:
        raise ValueError("unterminated code fence")
    flush()
    if not any(block.level == "L0" and block.section == "Overview" for block in blocks) or not any(block.level == "L1" for block in blocks):
        raise ValueError("missing L0 or L1 report sections")
    if not any(block.level == "L2" for block in blocks):
        raise ValueError("missing rag knowledge block")
    return meta, blocks


class ReportIndex:
    """Short-lived SQLite connections; one transaction per report replacement."""

    def __init__(self, storage_root: str | Path):
        self.storage_root = Path(storage_root).expanduser().resolve()
        self.reports_root = self.storage_root / "reports"
        self.path = self.storage_root / "rag" / "index.sqlite"

    def _connect(self, path: Path | None = None) -> sqlite3.Connection:
        target = path or self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            conn = sqlite3.connect(target, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS reports (
                    report_id TEXT PRIMARY KEY, report_path TEXT UNIQUE NOT NULL,
                    content_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS blocks (
                    id INTEGER PRIMARY KEY, report_id TEXT NOT NULL REFERENCES reports(report_id) ON DELETE CASCADE,
                    report_path TEXT NOT NULL, thread_id TEXT NOT NULL, project TEXT, cwd TEXT,
                    section TEXT NOT NULL, level TEXT NOT NULL, turn_start INTEGER, turn_end INTEGER,
                    content TEXT NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL,
                    search_text TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS blocks_fts USING fts5(search_text, content='blocks', content_rowid='id');
                CREATE TRIGGER IF NOT EXISTS blocks_ai AFTER INSERT ON blocks BEGIN
                    INSERT INTO blocks_fts(rowid, search_text) VALUES (new.id, new.search_text);
                END;
                CREATE TRIGGER IF NOT EXISTS blocks_ad AFTER DELETE ON blocks BEGIN
                    INSERT INTO blocks_fts(blocks_fts, rowid, search_text) VALUES ('delete', old.id, old.search_text);
                END;
            """)
            return conn
        except sqlite3.DatabaseError as exc:
            raise RuntimeError(f"report index is unavailable at {target}: {exc}; rebuild it from reports") from exc

    def _relative_path(self, path: str | Path) -> str:
        candidate = Path(path).absolute()
        try:
            rel = candidate.relative_to(self.reports_root)
        except ValueError as exc:
            raise ValueError("report is outside reports root") from exc
        if len(rel.parts) != 5 or not re.fullmatch(r"\d{4}", rel.parts[0]) or not all(re.fullmatch(r"\d{2}", x) for x in rel.parts[1:3]):
            raise ValueError("not a dated report path")
        if not rel.parts[3].startswith("report-") or rel.parts[4] != rel.parts[3] + ".md":
            raise ValueError("not a formal report file")
        if any(part.is_symlink() for part in (self.reports_root, *[self.reports_root.joinpath(*rel.parts[:i]) for i in range(1, 6)])):
            raise ValueError("symbolic links are not report sources")
        if not candidate.is_file() or not candidate.resolve().is_relative_to(self.reports_root):
            raise ValueError("report is missing or outside reports root")
        return rel.as_posix()

    def _index(self, conn: sqlite3.Connection, path: Path) -> None:
        rel = self._relative_path(path)
        text = path.read_text(encoding="utf-8")
        _, blocks = _parse(text, rel)
        digest = _hash(text)
        report_id = _hash(rel)
        with conn:
            current = conn.execute("SELECT content_hash FROM reports WHERE report_id=?", (report_id,)).fetchone()
            if current and current[0] == digest:
                return
            conn.execute("DELETE FROM blocks WHERE report_id=?", (report_id,))
            conn.execute("INSERT INTO reports(report_id,report_path,content_hash) VALUES(?,?,?) ON CONFLICT(report_id) DO UPDATE SET content_hash=excluded.content_hash", (report_id, rel, digest))
            conn.executemany("""INSERT INTO blocks(report_id,report_path,thread_id,project,cwd,section,level,turn_start,turn_end,content,content_hash,created_at,search_text)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", [(
                b.report_id, b.report_path, b.thread_id, b.project, b.cwd, b.section, b.level,
                b.turn_start, b.turn_end, b.content, b.content_hash, b.created_at, _search_text(b.content)
            ) for b in blocks])

    def index_report(self, path: str | Path) -> None:
        with self._connect() as conn:
            self._index(conn, Path(path))

    def rebuild(self) -> RebuildResult:
        """Build a fresh database, then atomically replace even a corrupt old index."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix=".index-", suffix=".sqlite", delete=False) as tmp:
            fresh = Path(tmp.name)
        errors: list[tuple[str, str]] = []
        count = 0
        try:
            with self._connect(fresh) as conn:
                for path in sorted(self.reports_root.rglob("*.md")) if self.reports_root.exists() else []:
                    try:
                        self._index(conn, path)
                        count += 1
                    except (ValueError, UnicodeError, OSError, yaml.YAMLError) as exc:
                        errors.append((str(path), str(exc)))
            os.replace(fresh, self.path)
        finally:
            fresh.unlink(missing_ok=True)
        return RebuildResult(count, tuple(errors))

    def search(self, query: str, *, project: str | None = None, cwd: str | Path | None = None, limit: int = 20) -> list[ReportBlock]:
        if not isinstance(query, str) or not 1 <= limit <= 100:
            raise ValueError("query must be text and limit must be between 1 and 100")
        expression, exact = _query_terms(query)
        if not expression or not self.path.exists():
            return []
        filters = ["blocks_fts MATCH ?"]
        args: list[object] = [expression]
        if project is not None:
            filters.append("b.project = ?")
            args.append(project)
        if cwd is not None:
            filters.append("b.cwd = ?")
            args.append(_cwd(str(cwd)))
        sql = ("SELECT b.*, bm25(blocks_fts) AS score FROM blocks_fts JOIN blocks b ON b.id=blocks_fts.rowid WHERE "
               + " AND ".join(filters) + " ORDER BY score, b.report_path, b.id")
        found: list[ReportBlock] = []
        seen: set[tuple[str, str, str]] = set()
        with self._connect() as conn:
            for row in conn.execute(sql, args):
                if len(found) >= limit:
                    break
                try:
                    path = self.reports_root / row["report_path"]
                    self._relative_path(path)
                    with path.open("r", encoding="utf-8") as source:
                        source.read(1)
                except (ValueError, OSError, UnicodeError):
                    continue
                if any(term not in row["content"].casefold() for term in exact):
                    continue
                key = (row["report_id"], row["section"], row["content_hash"])
                if key in seen:
                    continue
                seen.add(key)
                found.append(ReportBlock(**{name: row[name] for name in ReportBlock.__dataclass_fields__}))
        return found
