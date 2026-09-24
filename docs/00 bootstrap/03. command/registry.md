---

name: registry — CLI 命令元数据统一注册表。

description:

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

---

## 0. 结构树

### 0.1. 静态结构树

```
registry.py — CLI 命令元数据统一注册表
│
├─ CommandSpec(dataclass)                     ← 单条命令元数据
│   ├─ name: str                              ← 命令名（含 / 前缀）
│   ├─ description: str                       ← 描述（/help 与补全 display_meta 共用）
│   ├─ handler: Callable[..., Any]            ← 处理函数
│   └─ source: Literal["builtin","skill"]="builtin" ← 来源标记
│
└─ CommandRegistry                            ← 命令注册表
    ├─ __init__()                             ← 初始化
    │   ├─ _specs: list[CommandSpec]          ← 保序列表
    │   └─ _by_name: dict[str, CommandSpec]   ← 名称索引（O(1) 查询）
    │
    ├─ register(spec) -> None                 ← 注册命令
    │   ├─ _by_name[spec.name] = spec
    │   └─ 保序：已存在 → 原位替换；否则追加
    │
    ├─ register_skill(name, description, handler) -> None ← 预留接口
    │   └─ register(CommandSpec(..., source="skill"))
    │
    ├─ list_all() -> list[CommandSpec]        ← 全部命令（按注册顺序）
    │   └─ 返回副本（list(self._specs)）
    │
    └─ get(name) -> CommandSpec | None        ← 按名查询
        └─ _by_name.get(name)
```

---

### 0.2. 消费者视图

```
消费者                    使用接口                       用途
──────────────────────────────────────────────────────────────────────────
commands._cmd_help        registry.list_all()           逐条打印 name + description
commands.handle_command   registry.get(name)            分发到 spec.handler
command_completer         registry.list_all()           产出命令名补全候选
                                                         （display_meta=description）
──────────────────────────────────────────────────────────────────────────
共用点：CommandSpec.description —— /help 文案与补全 display_meta 同源
```

---

### 0.3. 运行时调用树

```
① 模块加载（commands.py 底部）
commands.py 加载时
    └─ _registry = CommandRegistry()
          └─ _registry.register(CommandSpec(...)) × 15
                ├─ _by_name[name] = spec
                └─ _specs.append(spec)（保序）

② 命令补全（prompt_toolkit 触发）
main._run_chat_async → PromptSession
    └─ SlashCommandCompleter(get_registry(), ...)
          └─ get_completions(document, event)
                └─ registry.list_all()
                      └─ 逐 spec 匹配 → yield Completion(display_meta=spec.description)

③ 命令分发（用户提交 / 命令）
main._run_chat_async
    └─ handle_command(prompt, ...)
          └─ spec = registry.get(name)
                ├─ None → "Unknown command" → False
                └─ 命中 → spec.handler(ctx)

④ 帮助（/help）
_cmd_help
    └─ registry.list_all()
          └─ 逐 spec 打印 name + description

⑤ 未来 skill loader（本期未接）
bootstrap（未来）
    └─ registry.register_skill(name, description, handler)
          └─ register(CommandSpec(..., source="skill"))
                └─ UI 层零改动（补全与 /help 自动包含）
```

---

### 0.4. 补充

**`registry.py` 是 CLI 命令的元数据注册表——`CommandSpec` 定义单条命令的形状，`CommandRegistry` 保序存储并提供查询，是 `/help`、补全菜单、命令分发三者的共同数据源。**

几条主线：

- **单一描述来源**：`CommandSpec.description` 同时供 `_cmd_help`（帮助文案）与 `SlashCommandCompleter`（`display_meta`）使用——这是本模块存在的**核心动因**（注释明确"避免两处维护"）。
- **三个消费者共用一份数据**：
  - `commands._cmd_help` → `list_all()` 打印；
  - `commands.handle_command` → `get(name)` 分发；
  - `command_completer` → `list_all()` 产候选。
- **保序 + 同名覆盖**：`_specs` 保注册顺序（决定 `/help` 输出顺序），`register` 遇同名**原位替换**——不改变顺序，也不产生重复项。
- **双索引结构**：`_specs`（有序，供 list_all）+ `_by_name`（dict，供 get O(1)）——两种访问模式各用合适结构。
- **`source` 是数据层标记**：`"builtin"` / `"skill"` 区分来源，但**本期 UI 不区分**——纯为未来 skill 系统预留。
- **`register_skill` 是接口先行**：注释明确"本期不被任何业务代码调用"，等 skill 系统落地时在 bootstrap 遍历注册即可，**UI 层零改动**——这正是"注册表"抽象的价值：新增命令来源不需要改 `/help` 或补全逻辑。
- **`list_all` 返回副本**：`list(self._specs)` 防止外部修改内部列表——保证注册表状态可控。
- **与 `commands` 的分工**：`commands.py` **定义并注册**命令（15 条 builtin），`registry.py` **存储并提供查询**——前者是"内容"，后者是"容器"。
- **升级动因**：从 `commands.py` 原有的硬编码 `handlers = {"/help": ...}` dict 升级而来——dict 只能存 handler，无法同时承载 description（供 /help 与补全共用），故引入 `CommandSpec` + `CommandRegistry`。