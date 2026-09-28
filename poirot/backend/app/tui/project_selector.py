"""Keyboard picker for projects and project threads in the TUI."""

from __future__ import annotations

from typing import ClassVar
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Static


class ProjectPicker(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("up", "move_up", "Previous", show=False),
        Binding("down", "move_down", "Next", show=False),
        Binding("enter", "choose", "Select", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    DEFAULT_CSS = """
    ProjectPicker { align: center middle; background: $background 50%; }
    ProjectPicker > #project-picker-list {
        width: 90%; max-width: 110; height: auto; max-height: 14;
        padding: 1 2; border: solid $accent; background: $surface;
    }
    """

    def __init__(self, entries: list[tuple[str, str]], *, title: str) -> None:
        super().__init__()
        self.entries = entries
        self.title = title
        self.selected = 0

    def compose(self) -> ComposeResult:
        yield Static(id="project-picker-list")

    def on_mount(self) -> None:
        self._render_options()

    def _render_options(self) -> None:
        if not self.entries:
            content = f"{self.title}\nNo saved items\nEsc to close"
        else:
            start = max(0, min(self.selected - 9, len(self.entries) - 10))
            rows = [f"{self.title}  ↑/↓ move  Enter select  Esc cancel"]
            for index, (_, label) in enumerate(self.entries[start : start + 10], start):
                rows.append(f"{'>' if index == self.selected else ' '} {label}")
            content = "\n".join(rows)
        self.query_one("#project-picker-list", Static).update(content)

    def action_move_up(self) -> None:
        self.selected = max(0, self.selected - 1)
        self._render_options()

    def action_move_down(self) -> None:
        self.selected = min(len(self.entries) - 1, self.selected + 1)
        self._render_options()

    def action_choose(self) -> None:
        if self.entries:
            self.dismiss(self.entries[self.selected][0])

    def action_cancel(self) -> None:
        self.dismiss(None)


class FilePicker(ModalScreen[Path | None]):
    """Keyboard picker for duplicate ``@filename`` references."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("up", "move_up", "Previous", show=False),
        Binding("down", "move_down", "Next", show=False),
        Binding("enter", "choose", "Select", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    DEFAULT_CSS = """
    FilePicker { align: center middle; background: $background 50%; }
    FilePicker > #file-picker-list {
        width: 90%; max-width: 110; height: auto; max-height: 14;
        padding: 1 2; border: solid $accent; background: $surface;
    }
    """

    def __init__(self, entries: list[Path], *, title: str) -> None:
        super().__init__()
        self.entries = entries
        self.title = title
        self.selected = 0

    def compose(self) -> ComposeResult:
        yield Static(id="file-picker-list")

    def on_mount(self) -> None:
        self._render_options()

    def _render_options(self) -> None:
        start = max(0, min(self.selected - 9, len(self.entries) - 10))
        rows = [f"{self.title}  ↑/↓ move  Enter select  Esc cancel"]
        for index, path in enumerate(self.entries[start : start + 10], start):
            rows.append(f"{'>' if index == self.selected else ' '} {path}")
        self.query_one("#file-picker-list", Static).update("\n".join(rows))

    def action_move_up(self) -> None:
        self.selected = max(0, self.selected - 1)
        self._render_options()

    def action_move_down(self) -> None:
        self.selected = min(len(self.entries) - 1, self.selected + 1)
        self._render_options()

    def action_choose(self) -> None:
        if self.entries:
            self.dismiss(self.entries[self.selected])

    def action_cancel(self) -> None:
        self.dismiss(None)
