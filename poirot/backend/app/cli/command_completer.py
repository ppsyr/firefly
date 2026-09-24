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

from typing import Callable

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

from poirot.backend.app.cli.registry import CommandRegistry

_SKILL_SUBCOMMANDS = ("list", "off", "enable", "disable", "install")


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
    ) -> None:
        """初始化。

        Args:
            registry: 命令注册表。
            skill_provider: 返回 active skill 名的回调，可选。
        """
        self._registry = registry
        self._skill_provider = skill_provider

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