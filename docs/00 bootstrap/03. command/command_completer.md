---

name: command_completer — ``/`` 命令模糊补全菜单 + ``/skill <name>`` skill 名补全。

description:

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

---

## 0. 结构树

### 0.1. 静态结构树

```
command_completer.py — / 命令模糊补全 + /skill <name> 补全
│
├─ _SKILL_SUBCOMMANDS: tuple                  ← /skill 子命令元组
│   └─ ("list", "off", "enable", "disable", "install")
│
└─ SlashCommandCompleter(Completer)           ← 补全器
    ├─ __init__(registry, skill_provider)     ← 初始化
    │   ├─ _registry: CommandRegistry          ← 命令注册表
    │   └─ _skill_provider: Callable | None    ← active skill 名提供者（可选）
    │
    └─ get_completions(document, complete_event)
        │
        ├─ word = get_word_before_cursor(WORD=True)
        │
        ├─ 【分支 A】word 以 "/" 开头 → 命令名补全
        │   └─ 遍历 _registry.list_all()
        │         └─ word_lower in spec.name.lower()（子串匹配）
        │               └─ yield Completion(text=spec.name,
        │                                   display=spec.name,
        │                                   display_meta=spec.description)
        │
        └─ 【分支 B】/skill 后参数位 → 子命令 / skill 名补全
            ├─ 判断：text_before_cursor.lstrip() 以 "/skill" 开头
            │         且第 6 位为空白
            ├─ 子命令优先：_SKILL_SUBCOMMANDS 前缀匹配
            │     └─ 有命中 → 只补子命令（display_meta="subcommand"）→ return
            └─ 无子命令命中 → skill 名补全
                  └─ _skill_provider() 取 names
                        └─ 前缀匹配 → yield Completion(display_meta="skill")
```

---

### 0.2. 补全触发条件视图

```
输入状态                             触发分支           候选来源
──────────────────────────────────────────────────────────────────────────────
word 以 "/" 开头                     A · 命令名         CommandRegistry.list_all()
                                     （子串匹配 name）

/skill  + 空白 + 参数位              B · 子命令优先     _SKILL_SUBCOMMANDS（前缀）
                                     B · skill 名兜底   _skill_provider()（前缀）

其他（无 / 前缀、非 /skill 参数位）   不补全             —
──────────────────────────────────────────────────────────────────────────────
说明：
- A 分支用子串匹配（"exp" 能命中 "/expert"）
- B 分支用前缀匹配（子命令与 skill 名均前缀）
- B 子命令优先：命中子命令则不补 skill 名（防 "enable" 被同名 skill 遮蔽）
```

---

### 0.3. 运行时调用树

```
① 初始化（main._run_chat_async）
main._run_chat_async
    └─ session = PromptSession(
          completer=SlashCommandCompleter(
              get_registry(),                    ← commands 模块注册表
              skill_provider=_skill_names_provider ← 闭包，读最新 runtime
          ),
          complete_while_typing=True,
          complete_style=CompleteStyle.COLUMN,
          style=Style([...]),                    ← 补全菜单配色
       )

② 用户输入触发补全（prompt_toolkit 调用）
prompt_toolkit
    └─ SlashCommandCompleter.get_completions(document, event)
          │
          ├─ word 以 "/" 开头？
          │     └─ 是 → 遍历 _registry.list_all()
          │           └─ 子串匹配 → yield Completion（display_meta=description）
          │
          └─ /skill 后参数位？
                ├─ 子命令前缀匹配（_SKILL_SUBCOMMANDS）
                │     └─ 命中 → yield Completion（subcommand）→ return
                └─ 无命中 → _skill_provider()
                      └─ _skill_names_provider()（闭包读 runtime.skill_manager）
                            └─ runtime.skill_manager.list_skills() → 名字列表
                      └─ 前缀匹配 → yield Completion（skill）

③ 补全菜单渲染（prompt_toolkit）
prompt_toolkit
    └─ 用 main.py 的 Style 渲染候选（选中/未选中配色）
          └─ display / display_meta 展示
```

---

### 0.4. 补充

**`command_completer.py` 是 prompt_toolkit 的补全器——只做"给定当前输入，产出补全候选"，命令名来自 `CommandRegistry`，skill 名来自注入的 `skill_provider`。**

几条主线：

- **两个补全分支**：① `word` 以 `/` 开头 → 命令名补全（**子串匹配**，`exp` 能命中 `/expert`）；② `/skill ` 后参数位 → **子命令优先，skill 名兜底**。
- **子命令优先是关键**：`/skill ` 后若 `enable` 等子命令有前缀命中，**只补子命令、不补 skill 名**——防止同名 skill 遮蔽子命令（注释明确"避免 enable 被 skill 名遮蔽"）。
- **匹配策略不同**：命令名用**子串**（宽松，便于模糊命中）；子命令与 skill 名用**前缀**（严格，符合"继续输入"的补全直觉）。
- **provider 容错**：`_skill_provider` 为 None 或抛异常时视为空列表，**不报错**——保证补全器在 skill 模块未启用时也能工作。
- **描述来源统一**：`display_meta=spec.description`——与 `/help` 共用 `CommandSpec.description`，避免两处维护。
- **配色不在本文件**：选中/未选中行配色由 `main.py` 的 `PromptSession` Style 配置——本文件只产出候选，不管渲染。
- **skill 名来自闭包**：`main` 注入的 `_skill_names_provider` 每次调用读**最新 runtime**（`switch` / `reload` 后 runtime 重绑定，闭包见新值），保证补全列表与实际状态一致。
- **与 commands 的分工**：`commands.py` 定义命令与注册表，`command_completer.py` 消费注册表做补全——注册表是两者之间的契约（`CommandSpec` 同时供 `/help` 与补全使用）。