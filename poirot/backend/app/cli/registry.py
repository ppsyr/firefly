"""registry — CLI 命令元数据统一注册表。

【整体职责】
把 commands.py 原本硬编码的 handlers dict 升级为 CommandRegistry，让 _cmd_help 文案
与 `/` 补全菜单（SlashCommandCompleter）共用同一份 CommandSpec.description，避免两处
维护。同时预留 register_skill 接口，为未来 skill 系统落地时注入命令预留接入点。

【内容摘要】
- CommandSpec     : 单条命令的元数据（name / description / handler / source）。
- CommandRegistry : 命令注册表，保序存储 CommandSpec，供补全菜单与 /help 共用。
- register        : 注册命令（同名覆盖，保序）。
- register_skill  : 预留接口，供未来 skill loader 注册 skill 命令。
- list_all        : 返回全部命令（按注册顺序）。
- get             : 按名查询（分发用）。

【职责边界】
- 只负责：命令元数据的存储与查询。
- 不负责：命令的实现（commands 的 _cmd_*）、补全候选的产出（command_completer）、
  命令的分发调度（commands.handle_command）。

【INVARIANT】
- 单一描述来源：CommandSpec.description 同时供 /help 文案与补全 display_meta 使用。
- 保序：注册顺序即 list_all() 返回顺序，/help 输出顺序由此决定。
- 同名覆盖：同名命令后注册者覆盖前者（原位替换，不改变顺序）。
- source 标记：区分 builtin 与 skill；本期仅数据层标记，UI 不区分。
- register_skill 本期无调用方：Poirot 暂无 CLI 侧 skill loader，接口先行。
- get 用 dict 索引：按名 O(1) 查询；list_all 返回副本（防外部修改内部列表）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal


@dataclass
class CommandSpec:
    """单条 `/` 命令的元数据。

    Attributes:
        name: 命令名（含 `/` 前缀，如 ``/help``）。
        description: 一句话描述，供 ``/help`` 文案与补全菜单 ``display_meta`` 共用。
        handler: 处理函数，签名与原 ``commands.py`` 里 ``_cmd_*`` 一致；
            返回 ``True`` 表示退出 CLI，``False`` 或 ``None`` 表示继续循环。
        source: ``"builtin"``（硬编码命令）或 ``"skill"``（未来 skill loader 注册）。
            本期仅用于数据层标记，不在 UI 上区分。
    """

    name: str
    description: str
    handler: Callable[..., Any]
    source: Literal["builtin", "skill"] = "builtin"


class CommandRegistry:
    """命令注册表——保序存储 ``CommandSpec``，供补全菜单与 ``/help`` 共用。

    注册顺序即 ``list_all()`` 返回顺序，``_cmd_help`` 输出顺序由此决定。

    Attributes:
        _specs: 保序的 CommandSpec 列表。
        _by_name: name → CommandSpec 索引，供 get 快速查询。
    """

    def __init__(self) -> None:
        """初始化空的注册表。"""
        self._specs: list[CommandSpec] = []
        self._by_name: dict[str, CommandSpec] = {}

    def register(self, spec: CommandSpec) -> None:
        """注册一条命令（builtin 或 skill）。同名命令后注册者覆盖前者。

        Args:
            spec: 命令元数据。
        """
        self._by_name[spec.name] = spec
        # 保序：若 name 已存在则替换原位，否则追加
        existing_idx = next(
            (i for i, s in enumerate(self._specs) if s.name == spec.name), None
        )
        if existing_idx is not None:
            self._specs[existing_idx] = spec
        else:
            self._specs.append(spec)

    def register_skill(self, name: str, description: str, handler: Callable[..., Any]) -> None:
        """预留接口：未来 skill loader 调此方法注册 skill 命令。

        等价于 ``register(CommandSpec(name, description, handler, source="skill"))``。
        本期无调用方——Poirot 还没有 CLI 侧 skill 发现/加载机制，做假数据会误导用户。

        Args:
            name: 命令名。
            description: 命令描述。
            handler: 处理函数。
        """
        self.register(CommandSpec(name=name, description=description, handler=handler, source="skill"))

    def list_all(self) -> list[CommandSpec]:
        """返回全部已注册命令（按注册顺序）。补全菜单与 ``/help`` 共用此数据源。

        Returns:
            list[CommandSpec]: 命令列表副本。
        """
        return list(self._specs)

    def get(self, name: str) -> CommandSpec | None:
        """按命令名查询；未命中返回 ``None``。``handle_command`` 分发用。

        Args:
            name: 命令名。

        Returns:
            CommandSpec | None: 命令元数据；未命中则 None。
        """
        return self._by_name.get(name)