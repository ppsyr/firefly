"""User-level storage and rendering for manual thread reports.

This module is deliberately independent from the run artifact store.  A manual
report is a durable snapshot under ``storage_root/reports``; automatic export
artifacts continue to live in the current run's output directory.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


_MAX_TITLE_LENGTH = 96


@dataclass(frozen=True)
class SavedReport:
    """Paths and identity of one persisted report snapshot."""

    report_path: str
    conversation_path: str
    title: str
    created_at: str


def _message_role(message: Any) -> str | None:
    value = getattr(message, "type", None)
    if value is None:
        value = getattr(message, "role", None)
    if value is None and isinstance(message, dict):
        value = message.get("type") or message.get("role")
    if value in {"human", "user"}:
        return "user"
    if value in {"ai", "assistant"}:
        return "assistant"
    return None


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return ""


def extract_conversation(messages: Iterable[Any]) -> list[dict[str, Any]]:
    """Return only user/assistant text messages with stable turn numbers."""
    rows: list[dict[str, Any]] = []
    turn = 0
    for message in messages:
        role = _message_role(message)
        if role is None:
            continue
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content")
        text = _message_text(content).strip()
        if not text:
            continue
        if role == "user":
            turn += 1
        rows.append({"turn": turn, "role": role, "content": text})
    return rows


def sanitize_title(value: str, *, fallback: str = "report") -> str:
    """Make a readable, bounded filename component without path semantics."""
    if not isinstance(value, str):
        raise ValueError("Report title must be text")
    text = unicodedata.normalize("NFC", value)
    text = text.replace("/", " ").replace("\\", " ").replace("..", " ")
    text = "".join(" " if unicodedata.category(ch) in {"Cc", "Cf"} else ch for ch in text)
    text = " ".join(text.split()).strip(" .")
    if not text:
        if not fallback:
            raise ValueError("Report title cannot be empty")
        text = fallback
    # Keep common punctuation and all normal Unicode letters/numbers.  Other
    # punctuation is harmless in a filename but replacing it makes paths more
    # predictable across platforms.
    text = "".join(ch if (ch.isalnum() or ch.isspace() or ch in "-_.(),[]{}!@#$%&+=;:'\"，。！？：；（）【】") else "_" for ch in text)
    text = " ".join(text.split()).strip(" .") or fallback
    return text[:_MAX_TITLE_LENGTH].rstrip(" .") or fallback


def _field(item: Any, name: str) -> Any:
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


def _content_list(items: Iterable[Any], field: str = "content", limit: int = 8) -> list[str]:
    result: list[str] = []
    for item in items:
        value = _field(item, field)
        if isinstance(value, str) and value.strip():
            result.append(" ".join(value.split()))
        if len(result) >= limit:
            break
    return result


def _strip_code_and_logs(text: str) -> str:
    lines: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or line.lstrip().startswith(("[tool]", "[tool:", "Traceback (most recent call last):")):
            continue
        if line.strip():
            lines.append(line.strip())
    return "\n".join(lines)


def render_layered_report(
    *,
    title: str,
    final_report: str,
    state: dict[str, Any],
    metadata: dict[str, Any],
    conversation_location: str,
) -> str:
    """Render frontmatter plus L0/L1/L2 sections from one state snapshot."""
    question = str(state.get("research_question") or state.get("user_input") or title)
    observations = state.get("observations") or []
    sources = state.get("sources") or []
    errors = state.get("errors") or []
    todos = state.get("todos") or []
    messages = state.get("messages") or []
    conversation = extract_conversation(messages)
    failures = [item for item in errors if _field(item, "kind") != "success"]
    status = "incomplete" if failures or todos else "complete"

    summary_text = " ".join(_strip_code_and_logs(final_report).split())
    summary_text = summary_text[:800] if summary_text else "No report result was collected."
    topic_values = [question]
    topic_values.extend(_content_list(sources, "title", limit=5))
    topics = ", ".join(dict.fromkeys(topic_values))

    frontmatter = ["---"]
    frontmatter_fields = {
        "name": title,
        "description": _report_description(question, summary_text, status),
        "thread_id": metadata.get("thread_id", ""),
        "project": metadata.get("project") or "",
        "cwd": metadata.get("cwd") or "",
        "report_created_at": metadata.get("report_created_at", ""),
        "thread_location": metadata.get("thread_location", ""),
        "conversation_location": conversation_location,
        "schema_version": "1",
        "location": metadata.get("report_location", ""),
    }
    for key, value in frontmatter_fields.items():
        frontmatter.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    frontmatter.append("---")

    lines = frontmatter + [
        "",
        f"# {title}",
        "",
        "## L0 Overview",
        "",
        f"- Goal: {question}",
        f"- Result: {summary_text}",
        f"- Status: {status}",
        f"- Topics: {topics}",
        f"- Open items: {len(todos) + len(failures)}",
        "",
        "## L1 Structured Summary",
        "",
        "### Key decisions and constraints",
    ]
    decisions = _content_list(state.get("reflection_items") or [], "content")
    if decisions:
        lines.extend(f"- {item}" for item in decisions)
    else:
        lines.append("- No explicit decisions were recorded.")
    lines.extend(["", "### Deliverables and sources"])
    deliverables = _content_list(observations, "content")
    if deliverables:
        lines.extend(f"- {item}" for item in deliverables)
    else:
        lines.append(f"- {summary_text}")
    for source in sources[:8]:
        source_title = _field(source, "title") or _field(source, "url")
        if source_title:
            lines.append(f"- Source: {source_title}")
    lines.extend(["", "### Known limits and follow-ups"])
    if todos:
        lines.extend(f"- {item}" for item in _content_list(todos, "content"))
    if failures:
        lines.extend(f"- Failed operation: {_field(item, 'reason') or _field(item, 'message') or 'unknown'}" for item in failures[:8])
    if not todos and not failures:
        lines.append("- No outstanding items were recorded.")
    lines.extend(["", "### Conversation rounds"])
    if conversation:
        for row in conversation:
            content = " ".join(row["content"].split())[:500]
            lines.append(f"- Turn {row['turn']} {row['role']}: {content}")
    else:
        lines.append("- No user/assistant text messages were recorded.")

    rag_items = [item for item in (_strip_code_and_logs(value) for value in deliverables) if item]
    if not rag_items:
        cleaned = _strip_code_and_logs(final_report)
        rag_items = [part.strip() for part in re.split(r"\n+|(?<=[.!?。！？])\s+", cleaned) if part.strip()][:8]
    lines.extend(["", "## L2 Knowledge", "", "```rag"])
    if rag_items:
        lines.extend(f"- {item}" for item in rag_items)
    else:
        lines.append("No independently retrievable knowledge was recorded.")
    lines.extend(["```", ""])
    return "\n".join(lines)


def _report_description(question: str, result: str, status: str) -> str:
    """Create the short description used by report frontmatter."""
    return f"目标：{question}；结果：{result[:500]}；状态：{status}"


class ReportStore:
    """Persist paired Markdown and conversation snapshots atomically."""

    def __init__(self, storage_root: str | Path):
        self.storage_root = Path(storage_root).expanduser().resolve()
        self.root = self.storage_root / "reports"

    def save(
        self,
        *,
        title: str,
        final_report: str,
        state: dict[str, Any],
        metadata: dict[str, Any],
        created_at: datetime | None = None,
    ) -> SavedReport:
        when = created_at or datetime.now().astimezone()
        if when.tzinfo is None:
            when = when.astimezone()
        created_at_text = when.isoformat()
        safe_title = sanitize_title(title)
        day_dir = self.root / when.strftime("%Y/%m/%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        stem_base = f"report-{when:%H-%M-%S}-{safe_title}"
        stem = stem_base
        counter = 1
        while (day_dir / stem).exists():
            counter += 1
            stem = f"{stem_base}-{counter}"
        report_dir = day_dir / stem
        report_dir.mkdir()
        report_path = report_dir / f"{stem}.md"
        conversation_path = report_dir / f"{stem}.conversation.jsonl"
        metadata = dict(metadata)
        metadata["report_created_at"] = created_at_text
        metadata["conversation_location"] = str(conversation_path)
        metadata["report_location"] = str(report_dir)
        report_text = render_layered_report(
            title=safe_title,
            final_report=final_report,
            state=state,
            metadata=metadata,
            conversation_location=str(conversation_path),
        )
        rows = extract_conversation(state.get("messages") or [])
        conversation_text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
        report_temp: Path | None = None
        conversation_temp: Path | None = None
        report_written = False
        conversation_written = False
        try:
            report_temp = self._write_temp(report_path, report_text)
            conversation_temp = self._write_temp(conversation_path, conversation_text)
            os.replace(report_temp, report_path)
            report_written = True
            os.replace(conversation_temp, conversation_path)
            conversation_written = True
            self._fsync_directory(day_dir)
        except Exception:
            if report_written:
                report_path.unlink(missing_ok=True)
            if conversation_written:
                conversation_path.unlink(missing_ok=True)
            try:
                report_dir.rmdir()
            except OSError:
                pass
            raise
        finally:
            if report_temp is not None:
                report_temp.unlink(missing_ok=True)
            if conversation_temp is not None:
                conversation_temp.unlink(missing_ok=True)
        return SavedReport(str(report_path), str(conversation_path), safe_title, created_at_text)

    @staticmethod
    def _write_temp(path: Path, content: str) -> Path:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".report-", delete=False) as file:
            temp = Path(file.name)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        return temp

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
