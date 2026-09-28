"""A bounded, scrolling prompt_toolkit thread selector."""
from __future__ import annotations

import shutil
import sys
from wcwidth import wcswidth

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl


async def select_thread(items, current_id: str, console, *, input=None, output=None, interactive=None) -> str | None:
    if not items:
        console.print("[dim]No saved threads[/dim]")
        return None
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not interactive:
        for item in items:
            mark = "*" if item.thread_id == current_id else " "
            console.print(f"{mark} {item.title} [{item.thread_id}]", markup=False)
        console.print("Use /thread switch <id> to restore a thread")
        return None

    selected = 0
    width = max(10, shutil.get_terminal_size().columns - 2)
    visible = max(1, min(10, len(items), shutil.get_terminal_size().lines - 3))

    def render():
        top = max(0, min(selected - visible + 1, len(items) - visible))
        lines = [("class:hint", "Select thread: ↑/↓ move, Enter restore, Esc cancel\n")]
        for index in range(top, top + visible):
            item = items[index]
            marker = "*" if item.thread_id == current_id else " "
            id_width = max(2, min(12, width // 4))
            suffix = f" [{item.thread_id[:id_width]}]"
            title = item.title
            allowed = max(1, width - len(suffix) - 3)
            if wcswidth(title) > allowed:
                kept = ""
                for char in title:
                    if wcswidth(kept + char) > allowed - 1:
                        break
                    kept += char
                title = kept + "…"
            style = "reverse" if index == selected else ""
            lines.append((style, f"{'>' if index == selected else ' '} {marker} {title}{suffix}\n"))
        return FormattedText(lines)

    keys = KeyBindings()

    @keys.add("up")
    def up(event):
        nonlocal selected
        selected = max(0, selected - 1)
        event.app.invalidate()

    @keys.add("down")
    def down(event):
        nonlocal selected
        selected = min(len(items) - 1, selected + 1)
        event.app.invalidate()

    @keys.add("enter")
    def choose(event):
        event.app.exit(result=items[selected].thread_id)

    @keys.add("escape")
    @keys.add("c-c")
    def cancel(event):
        event.app.exit(result=None)

    app = Application(
        layout=Layout(Window(FormattedTextControl(render), height=visible + 1)),
        key_bindings=keys,
        full_screen=False,
        input=input,
        output=output,
    )
    return await app.run_async()
