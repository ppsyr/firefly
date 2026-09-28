"""Same-directory thread history references.

Thread quoting is deliberately kept separate from thread switching.  A quote
only reads the target's latest committed checkpoint and returns material for
the current prompt; it never changes either runtime's active thread.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from poirot.backend.agents.context_engineering.utilities import token_counter


MAX_QUOTED_TURNS = 10
MAX_QUOTED_TOKENS = 4_000


class ThreadQuoteError(ValueError):
    """Base class for user-facing thread quote failures."""


class ThreadQuoteNotFound(ThreadQuoteError):
    """A requested thread or title did not resolve."""


class ThreadQuoteAmbiguous(ThreadQuoteError):
    """A short ID or title matched more than one same-directory thread."""


class ThreadQuoteUnavailable(ThreadQuoteError):
    """The target exists but cannot be safely read or is not eligible."""


@dataclass(frozen=True)
class ParsedThreadReference:
    value: str
    start: int
    end: int
    quoted_title: bool = False


@dataclass(frozen=True)
class QuotedMessage:
    role: str
    content: str


@dataclass(frozen=True)
class ThreadQuote:
    reference: str
    thread_id: str
    title: str
    messages: tuple[QuotedMessage, ...]
    truncated: bool = False

    def render(self) -> str:
        lines = [f"[Referenced thread: {self.title} ({self.thread_id})]"]
        for message in self.messages:
            lines.append(f"[{message.role}]\n{message.content}")
        if self.truncated:
            lines.append("[Referenced thread history truncated to the recent 10 turns and 4,000 tokens]")
        lines.append("[End referenced thread]")
        return "\n".join(lines)


@dataclass(frozen=True)
class ThreadQuoteResult:
    question: str
    masked_question: str
    quotes: tuple[ThreadQuote, ...] = ()

    @property
    def enriched_suffix(self) -> str:
        if not self.quotes:
            return ""
        return "\n\n".join(quote.render() for quote in self.quotes)


@dataclass(frozen=True)
class ThreadCandidate:
    """A same-directory candidate shown while the user is typing."""

    thread_id: str
    title: str
    insert_text: str
    display: str


# Quoted titles are intentionally explicit.  Bare values are resolved only
# when they match an existing ID/prefix/title, so ordinary @mentions remain
# ordinary prose and are left for the file reference parser.
_QUOTED_RE = re.compile(r'(?<![\w@])@"([^"\r\n]*)"')
# Bare references may be a complete one-word title as well as an ID.  The
# resolver still requires an exact saved title, so ordinary @mentions and file
# paths remain untouched.
_BARE_RE = re.compile(r"(?<![\w@])@([^\s@()<>]+)")


def parse_thread_references(question: str) -> list[ParsedThreadReference]:
    """Parse explicit ``@"title"`` and ID-shaped ``@value`` references."""
    found: list[ParsedThreadReference] = [
        ParsedThreadReference(match.group(1), match.start(), match.end(), True)
        for match in _QUOTED_RE.finditer(question)
    ]
    quoted_ranges = [(item.start, item.end) for item in found]
    for match in _BARE_RE.finditer(question):
        if any(start <= match.start() < end for start, end in quoted_ranges):
            continue
        found.append(ParsedThreadReference(match.group(1), match.start(), match.end()))
    return sorted(found, key=lambda item: item.start)


def thread_reference_fragment_at_cursor(
    text: str, cursor: int | None = None
) -> tuple[str, int, bool] | None:
    """Return ``(fragment, start, quoted_title)`` for a live ``@thread`` token."""
    cursor = len(text) if cursor is None else max(0, min(cursor, len(text)))
    before = text[:cursor]
    quoted = re.search(r'(?<![\w@])@"([^"\r\n]*)$', before)
    if quoted:
        return quoted.group(1), quoted.start(), True
    bare = re.search(r"(?<![\w@])@([^\s@()<>]*)$", before)
    if bare:
        return bare.group(1), bare.start(), False
    # An isolated @ starts a new candidate search.
    if re.search(r"(?<![\w@])@$", before):
        return "", len(before) - 1, False
    return None


def thread_candidates(
    fragment: str,
    *,
    current: Any,
    thread_store: Any,
    quoted_title: bool = False,
    limit: int = 50,
) -> list[ThreadCandidate]:
    """Synchronously list same-directory candidates for an in-progress token."""
    current_dir = _canonical_dir(getattr(current, "cwd", None))
    if current_dir is None:
        return []
    try:
        items = thread_store.list()
    except Exception:
        return []
    value = fragment or ""
    candidates: list[ThreadCandidate] = []
    for item in items:
        if item.thread_id == getattr(current, "thread_id", None):
            continue
        if not _same_directory(current, item):
            continue
        if quoted_title:
            if value.casefold() not in item.title.casefold():
                continue
            insert = f'@"{item.title}" '
        else:
            # A slash or extension strongly indicates a file path. Avoid
            # flooding the file picker with thread rows in that case.
            if "/" in value or "." in value:
                continue
            needle = value.casefold()
            if not (item.thread_id.casefold().startswith(needle) or needle in item.title.casefold()):
                continue
            insert = f"@{item.thread_id} "
        candidates.append(ThreadCandidate(item.thread_id, item.title, insert, f"{item.title} [{item.thread_id}]"))
    return candidates[:limit]


def mask_thread_references(question: str, references: Iterable[ParsedThreadReference]) -> str:
    """Blank resolved references while preserving offsets and user text."""
    chars = list(question)
    for reference in references:
        for index in range(reference.start, reference.end):
            if chars[index] not in "\r\n":
                chars[index] = " "
    return "".join(chars)


def _canonical_dir(value: str | Path | None) -> Path | None:
    if not value:
        return None
    try:
        return Path(value).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _message_text(content: Any) -> str:
    """Keep text blocks only; never stringify tool args or other payloads."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        text = content.get("text")
        return text if content.get("type") in (None, "text") and isinstance(text, str) else ""
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str) and (part.get("type") in (None, "text") or "type" not in part):
                parts.append(text)
    return "".join(parts)


def _message_role(message: Any) -> str | None:
    value = getattr(message, "type", None)
    if value is None and isinstance(message, dict):
        value = message.get("type") or message.get("role")
    if value in ("human", "user"):
        return "User"
    if value in ("ai", "assistant"):
        return "Assistant"
    return None


def _extract_messages(checkpoint: Any) -> list[QuotedMessage]:
    if checkpoint is None:
        raise ThreadQuoteUnavailable("Referenced thread has no committed checkpoint")
    values = getattr(checkpoint, "checkpoint", checkpoint)
    if not isinstance(values, dict):
        raise ThreadQuoteUnavailable("Referenced thread checkpoint is unreadable")
    values = values.get("channel_values", values)
    messages = values.get("messages") if isinstance(values, dict) else None
    if not isinstance(messages, (list, tuple)):
        raise ThreadQuoteUnavailable("Referenced thread history is unreadable")
    result: list[QuotedMessage] = []
    for message in messages:
        role = _message_role(message)
        if role is None:
            continue
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content")
        text = _message_text(content)
        if text:
            result.append(QuotedMessage(role, text))
    return result


def _fit_messages(messages: list[QuotedMessage]) -> tuple[list[QuotedMessage], bool]:
    """Keep the latest ten user turns and fit the result to the token cap."""
    if not messages:
        return [], False
    starts = [index for index, message in enumerate(messages) if message.role == "User"]
    if starts:
        start = starts[max(0, len(starts) - MAX_QUOTED_TURNS)]
        selected = messages[start:]
    else:
        selected = messages[-MAX_QUOTED_TURNS:]
    truncated = len(selected) != len(messages)

    # A turn is a user message and all following assistant messages until the
    # next user message. Drop oldest complete turns first.
    while len(selected) > 1 and token_counter(selected) > MAX_QUOTED_TOKENS:
        next_user = next((i for i, item in enumerate(selected[1:], 1) if item.role == "User"), None)
        if next_user is None:
            break
        selected = selected[next_user:]
        truncated = True

    if token_counter(selected) > MAX_QUOTED_TOKENS:
        # Preserve the newest material. Binary-search the suffix of each
        # message in order, which also works with CJK-aware token counting.
        adjusted = list(selected)
        for index, item in enumerate(adjusted):
            if token_counter(adjusted) <= MAX_QUOTED_TOKENS:
                break
            text = item.content
            low, high = 0, len(text)
            while low < high:
                cut = (low + high + 1) // 2
                candidate = text[-cut:]
                trial = adjusted[:index] + [QuotedMessage(item.role, candidate)] + adjusted[index + 1:]
                if token_counter(trial) <= MAX_QUOTED_TOKENS:
                    low = cut
                else:
                    high = cut - 1
            adjusted[index] = QuotedMessage(item.role, adjusted[index].content[-low:] if low else "")
        selected = [item for item in adjusted if item.content]
        truncated = True
    return selected, truncated


def _same_directory(current: Any, target: Any) -> bool:
    current_dir = _canonical_dir(getattr(current, "cwd", None))
    target_dir = _canonical_dir(getattr(target, "cwd", None))
    return current_dir is not None and target_dir is not None and current_dir == target_dir


def _display_candidates(items: Iterable[Any]) -> str:
    return ", ".join(f"{item.thread_id} ({item.title})" for item in items)


def _resolve_one(
    reference: ParsedThreadReference,
    *,
    current: Any,
    items: list[Any],
    file_name_conflict: Callable[[str], bool] | None,
) -> Any | None:
    value = reference.value
    explicit_id = value.startswith("thread-")
    exact_id = next((item for item in items if item.thread_id == value), None)
    id_value = value[7:] if explicit_id else value
    if exact_id is None and explicit_id:
        exact_id = next((item for item in items if item.thread_id == id_value), None)

    if exact_id is not None:
        if exact_id.thread_id == getattr(current, "thread_id", None):
            raise ThreadQuoteError("A thread cannot quote itself")
        if not _same_directory(current, exact_id):
            raise ThreadQuoteUnavailable("Referenced thread is not in the current directory")
        return exact_id

    same_dir = [item for item in items if _same_directory(current, item) and item.thread_id != getattr(current, "thread_id", None)]
    id_matches = [item for item in same_dir if item.thread_id.startswith(id_value)] if id_value else []
    title_matches = [item for item in same_dir if item.title == value]
    matches = {item.thread_id: item for item in id_matches + title_matches}
    if not matches:
        if reference.quoted_title or explicit_id:
            raise ThreadQuoteNotFound(f"No same-directory thread matches @{value}")
        return None
    if len(matches) > 1:
        raise ThreadQuoteAmbiguous(
            f"@{value} is ambiguous; use a complete thread ID or @\"title\". Candidates: {_display_candidates(matches.values())}"
        )
    target = next(iter(matches.values()))
    # A bare title that is also an existing filename must be explicit. Full
    # IDs remain deterministic and are preferred over the file interpretation.
    if title_matches and not id_matches and not reference.quoted_title and file_name_conflict and file_name_conflict(value):
        raise ThreadQuoteAmbiguous(f"@{value} is both a thread title and a file; use @\"{value}\" or the complete thread ID")
    return target


def quote_threads(
    question: str,
    *,
    current: Any,
    thread_store: Any,
    checkpointer: Any,
    file_name_conflict: Callable[[str], bool] | None = None,
) -> ThreadQuoteResult:
    """Resolve and read all same-directory thread references in ``question``."""
    parsed = parse_thread_references(question)
    if not parsed:
        return ThreadQuoteResult(question, question)
    current_dir = _canonical_dir(getattr(current, "cwd", None))
    try:
        items = list(thread_store.list())
    except Exception as exc:
        raise ThreadQuoteUnavailable("Unable to inspect saved threads") from exc
    if current_dir is None or not current_dir.is_dir():
        # A bare @word is also valid prose and may be a file reference. Only
        # reject it on an unbound thread when it actually resembles a saved
        # thread; explicit prefixes/titles are always treated as requests.
        possible = any(
            reference.quoted_title
            or reference.value.startswith("thread-")
            or any(
                item.thread_id == reference.value
                or item.thread_id.startswith(reference.value)
                or item.title == reference.value
                for item in items
            )
            for reference in parsed
        )
        if possible:
            raise ThreadQuoteUnavailable("Current thread is not bound to a directory")
        return ThreadQuoteResult(question, question)

    resolved_refs: list[ParsedThreadReference] = []
    quotes: list[ThreadQuote] = []
    for reference in parsed:
        target = _resolve_one(reference, current=current, items=items, file_name_conflict=file_name_conflict)
        if target is None:
            continue
        try:
            checkpoint = checkpointer.get_tuple({"configurable": {"thread_id": target.thread_id}})
            messages, truncated = _fit_messages(_extract_messages(checkpoint))
        except ThreadQuoteError:
            raise
        except Exception as exc:
            raise ThreadQuoteUnavailable(f"Unable to read referenced thread {target.thread_id}") from exc
        if not messages:
            raise ThreadQuoteUnavailable(f"Referenced thread {target.thread_id} has no readable user/assistant history")
        resolved_refs.append(reference)
        quotes.append(ThreadQuote(reference.value, target.thread_id, target.title, tuple(messages), truncated))

    return ThreadQuoteResult(question, mask_thread_references(question, resolved_refs), tuple(quotes))


__all__ = [
    "MAX_QUOTED_TURNS",
    "MAX_QUOTED_TOKENS",
    "ParsedThreadReference",
    "QuotedMessage",
    "ThreadQuote",
    "ThreadQuoteAmbiguous",
    "ThreadQuoteError",
    "ThreadQuoteNotFound",
    "ThreadQuoteResult",
    "ThreadQuoteUnavailable",
    "ThreadCandidate",
    "mask_thread_references",
    "parse_thread_references",
    "quote_threads",
    "thread_candidates",
    "thread_reference_fragment_at_cursor",
]
