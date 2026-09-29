"""command_completer — ``/`` 命令模糊补全菜单 + ``/skill <name>`` skill 名补全。

【整体职责】
为 prompt_toolkit 提供斜杠命令补全：消费 CommandRegistry.list_all()，在输入以 ``/``
开头时补全命令名；在 ``/skill `` 后补全子命令（list/off/enable/disable/install）或
active skill 名。选中/未选中行配色由 main.py 的 PromptSession Style 配置。

【内容摘要】
- _SKILL_SUBCOMMANDS  : /skill 的子命令元组。
- SlashCommandCompleter : 补全器实现，get_completions 分派命令名/子命令/skill 名补全。

【职责边界】
- 只负责：根据当前输入产出补全候选（Completion）。
- 不负责：命令注册与描述（registry / commands）、补全菜单的渲染与配色
  （prompt_toolkit + main.py 的 Style）、skill 名的实际来源（skill_provider 由 main 注入）。

【INVARIANT】
- 仅 ``/`` 触发：word 不以 ``/`` 开头时不做命令名补全。
- 子命令优先：``/skill `` 后若有子命令前缀命中，只补子命令（防 enable 被 skill 名遮蔽）。
- skill 名兜底：无子命令命中时才补 skill_provider() 返回的名（前缀匹配）。
- provider 容错：skill_provider 为 None 或抛异常时视为无 skill 名（不报错）。
- 子串匹配命令名：word_lower in spec.name.lower()（非前缀）。
- 描述复用：display_meta 用 spec.description，与 /help 共用同一来源。
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Callable

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

from poirot.backend.app.cli.registry import CommandRegistry

_SKILL_SUBCOMMANDS = ("list", "off", "enable", "disable", "install")
_THREAD_SUBCOMMANDS = ("info", "list", "new", "switch", "rename", "delete")
_PROJECT_SUBCOMMANDS = ("list",)
_ADD_DIR_OPTIONS = ("--remove",)


class SlashCommandCompleter(Completer):
    """``/`` 命令补全 + ``/skill <name>`` skill 名补全。

    - word 以 ``/`` 开头 → 命令名子串补全
    - ``/skill `` 后参数词 → 子命令前缀补全（优先）；无子命令命中 → skill 名前缀补全
    - skill_provider 默认 None（无 skill 名补全，兼容既有）

    Attributes:
        _registry: 命令注册表。
        _skill_provider: 提供 active skill 名的回调，可选。
    """

    def __init__(
        self,
        registry: CommandRegistry,
        skill_provider: Callable[[], list[str]] | None = None,
        file_provider: Callable[[str], list[tuple[Path, str]]] | None = None,
        thread_provider: Callable[[str, bool], list[tuple[str, str, str]]] | None = None,
        directory_provider: Callable[[], list[str]] | None = None,
        cd_directory_provider: Callable[[str], list[tuple[str, str]]] | None = None,
    ) -> None:
        """初始化。

        Args:
            registry: 命令注册表。
            skill_provider: 返回 active skill 名的回调，可选。
        """
        self._registry = registry
        self._skill_provider = skill_provider
        self._file_provider = file_provider
        self._thread_provider = thread_provider
        self._directory_provider = directory_provider
        self._cd_directory_provider = cd_directory_provider

    def get_completions(self, document: Document, complete_event):  # type: ignore[no-untyped-def]
        """产出补全候选。

        Args:
            document: 当前输入文档。
            complete_event: 补全事件（未使用）。

        Yields:
            Completion: 命令名 / 子命令 / skill 名候选。
        """
        word = document.get_word_before_cursor(WORD=True)

        # 命令名补全：仅 / 开头
        if word.startswith("/"):
            word_lower = word.lower()
            for spec in self._registry.list_all():
                if word_lower in spec.name.lower():
                    yield Completion(
                        text=spec.name,
                        start_position=-len(word),
                        display=spec.name,
                        display_meta=spec.description,
                    )
            return

        # /skill <arg> 补全：行以 /skill + 空白 开头，cursor 在参数位
        stripped = document.text_before_cursor.lstrip()
        if stripped.startswith("/thread") and len(stripped) > 7 and stripped[7].isspace():
            arg = stripped[8:]
            if not arg.strip() or len(arg.split()) <= 1 and not arg.endswith(" "):
                for sub in _THREAD_SUBCOMMANDS:
                    if sub.startswith(word.lower()):
                        yield Completion(sub, start_position=-len(word), display_meta="thread command")
            return
        if stripped.startswith("/project_thread") and len(stripped) > 15 and stripped[15].isspace():
            for sub in _PROJECT_SUBCOMMANDS:
                if sub.startswith(word.lower()):
                    yield Completion(sub, start_position=-len(word), display_meta="project thread command")
            return
        if stripped.startswith("/project") and len(stripped) > 8 and stripped[8].isspace():
            for sub in _PROJECT_SUBCOMMANDS:
                if sub.startswith(word.lower()):
                    yield Completion(sub, start_position=-len(word), display_meta="project command")
            return
        if stripped.startswith("/add-dir") and len(stripped) > 8 and stripped[8].isspace():
            if stripped.startswith("/add-dir --remove"):
                fragment = word.lower()
                try:
                    directories = self._directory_provider() if self._directory_provider else []
                except Exception:
                    directories = []
                for directory in directories:
                    if fragment and not directory.lower().startswith(fragment):
                        continue
                    text = shlex.quote(directory) + " "
                    yield Completion(text, start_position=-len(word), display=directory, display_meta="reference directory")
            elif word.startswith("-"):
                for option in _ADD_DIR_OPTIONS:
                    if option.startswith(word):
                        yield Completion(option, start_position=-len(word), display_meta="add-dir option")
            return
        if stripped.startswith("/cd") and len(stripped) > 3 and stripped[3].isspace():
            if self._cd_directory_provider is None:
                return
            fragment = word
            try:
                candidates = self._cd_directory_provider(fragment) or []
            except Exception:
                candidates = []
            for insert, display in candidates:
                yield Completion(
                    text=insert,
                    start_position=-len(fragment),
                    display=display,
                    display_meta="directory",
                )
            return
        if stripped.startswith("/skill") and len(stripped) > 6 and stripped[6] in (" ", "\t"):
            arg_word = word
            arg_lower = arg_word.lower()
            # 子命令优先：有命中则只补子命令，不补 skill 名（避免 enable 被 skill 名遮蔽）
            sub_matches = [s for s in _SKILL_SUBCOMMANDS if s.startswith(arg_lower)]
            if sub_matches:
                for s in sub_matches:
                    yield Completion(
                        text=s,
                        start_position=-len(arg_word),
                        display=s,
                        display_meta="subcommand",
                    )
                return
            # 无子命令命中 → 补 active skill 名
            if self._skill_provider is not None:
                try:
                    names = self._skill_provider() or []
                except Exception:
                    names = []
                for n in names:
                    if n.lower().startswith(arg_lower):
                        yield Completion(
                            text=n,
                            start_position=-len(arg_word),
                            display=n,
                            display_meta="skill",
                        )

        # Same-directory thread references are completed while the user is
        # still typing. Quoted titles use ``@"title"`` and are inserted as a
        # complete token so the user can continue typing after the trailing
        # space. The normal Enter handling accepts the highlighted completion.
        if self._thread_provider is not None:
            title_match = re.search(r'(?<![\w@])@"([^"\r\n]*)$', document.text_before_cursor)
            # Keep the token broad enough for Unicode and ordinary one-word
            # titles. The provider filters paths and enforces same-directory
            # eligibility, so this does not turn every @mention into a hit.
            thread_match = re.search(r"(?<![\w@])@([^\s@()<>]*)$", document.text_before_cursor)
            if title_match or thread_match:
                quoted = title_match is not None
                fragment = (title_match or thread_match).group(1) or ""
                token_start = (title_match or thread_match).start()
                try:
                    candidates = self._thread_provider(fragment, quoted) or []
                except Exception:
                    candidates = []
                for thread_id, label, insert in candidates:
                    yield Completion(
                        text=insert,
                        start_position=token_start - len(document.text_before_cursor),
                        display=label,
                        display_meta="thread",
                    )

        # File references are completed while the user is still typing.  The
        # provider performs the thread-scoped path check; this class only
        # presents its bounded results to prompt_toolkit.
        if self._file_provider is not None:
            match = re.search(r"(?<![\w@])@([^\s@()]*)$", document.text_before_cursor)
            if match and match.group(1):
                token = match.group(0)
                try:
                    candidates = self._file_provider(match.group(1)) or []
                except Exception:
                    candidates = []
                for path, label in candidates:
                    yield Completion(
                        # A confirmed reference must be delimited before the
                        # user continues typing (otherwise ``@main.py帮我看``
                        # is parsed as one filename).
                        text="@" + label + " ",
                        start_position=-len(token),
                        display="@" + label,
                        display_meta="file" if Path(path).is_file() else "directory",
                    )
