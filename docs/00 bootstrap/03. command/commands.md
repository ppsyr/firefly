---

name: commands — CLI 命令系统（/help /clear /expert /default /report /exit /expand /thinking /tools /model /thread /prompt /skill /mcp）。

description:

【整体职责】
提供 CLI 的斜杠命令体系：以 `/` 开头的输入由 handle_command 从 CommandRegistry 查
handler 分发，各 handler 统一签名（接收 CommandContext，返回 bool | None）。
返回 True 表示退出 CLI，False/None 表示继续循环。

【内容摘要】
- CommandContext        : 单次命令调用的上下文，统一 handler 签名。
- _cmd_help / _cmd_clear / _cmd_expert / _cmd_default / _cmd_report / _cmd_exit
- _cmd_expand / _cmd_thinking / _cmd_tools / _cmd_model / _cmd_thread / _cmd_prompt
- _cmd_skill / _cmd_mcp : 技能与 MCP 的子命令族。
- _registry            : 模块级命令注册表（注册全部 builtin 命令）。
- get_registry         : 供 main.py 构造补全器时取注册表。
- handle_command       : 命令分发入口。

【职责边界】
- 只负责：命令解析与分发、命令的呈现（console 输出）、设置 pending_* 标志。
- 不负责：命令触发的实际动作（切换模式 / 报告合成 / MCP 重载 / 模型切换由 main 主循环
  消费 pending_* 后执行）、补全实现（command_completer）、注册表实现（registry）。

---

## 0. 结构树

### 0.1. 静态结构树

```
commands.py — CLI 命令系统（/help /clear /expert /default /report /exit /expand /thinking /tools /model /thread /prompt /skill /mcp）
│
├─ CommandContext(dataclass)                 ← 命令调用上下文（统一 handler 签名）
│   ├─ console: Console
│   ├─ renderer: StreamRenderer
│   ├─ state: dict[str, Any]
│   ├─ runtime: Any
│   └─ arg: str
│
├─ 【基础命令 handler】
│   ├─ _cmd_help      → 列出全部命令
│   ├─ _cmd_clear     → 清屏
│   ├─ _cmd_expert    → state["pending_expert_mode"]=True
│   ├─ _cmd_default   → state["pending_expert_mode"]=False
│   ├─ _cmd_report    → state["pending_report"]=arg
│   ├─ _cmd_exit      → return True（退出）
│   ├─ _cmd_expand    → renderer.expand_last_round()
│   ├─ _cmd_thinking  → renderer.state["thinking_enabled"] 切换
│   └─ _cmd_tools     → get_available_tools(include_mcp=True) 列表
│
├─ 【模型 / 线程 / 提示词】
│   ├─ _cmd_model     → 无参显示路由链；有参设 pending_model_switch
│   ├─ _cmd_thread    → 显示 thread_id / thread_dir / 最近 run
│   └─ _cmd_prompt    → list | show <cat/name> | reload
│
├─ 【Skill 命令族】_cmd_skill
│   ├─ list                       ← 列活跃 skill（含 eff/sel/tools）
│   ├─ search <query>             ← hub unified_search（降级 builtin）
│   ├─ <name>                     ← 设 skill_override
│   ├─ off                        ← 清 skill_override
│   ├─ enable/disable <name>      ← store.set_enabled（持久）
│   ├─ evolve <name>              ← 手动 FIX 进化
│   ├─ capture <pattern> <name>   ← 手动 CAPTURED 沉淀
│   ├─ history <name>             ← evolution 历史
│   ├─ health [name]              ← RuntimeTracker 健康报告
│   ├─ eval-history <name>        ← judgments 历史
│   └─ install <path|identifier> [name] ← 本地或 remote（hub Installer）
│
├─ 【MCP 命令族】_cmd_mcp
│   ├─ list   ← servers + transport + 工具数 + 健康
│   └─ reload ← 设 pending_mcp_reload
│
├─ _registry: CommandRegistry                 ← 模块级注册表（注册 15 条命令）
│   ├─ /help /clear /expert /default /report /exit /quit
│   ├─ /expand /thinking /tools /model /thread /prompt
│   └─ /skill /mcp
│
├─ get_registry() -> CommandRegistry          ← 供 main 构造补全器
│
└─ handle_command(cmd, console, renderer, state, runtime) -> bool ← 分发入口
    ├─ 拆分 name / arg
    ├─ _registry.get(name)
    ├─ 未命中 → "Unknown command" → False
    └─ 命中 → 构造 CommandContext → spec.handler(ctx)
          └─ result is bool → result，否则 False
```

---

### 0.2. 命令 → 动作 视图

```
命令                 handler        动作 / pending 标志                    主循环后续
──────────────────────────────────────────────────────────────────────────────────────
/help                _cmd_help      列出全部命令                          无
/clear               _cmd_clear     清屏                                  无
/expert              _cmd_expert    state["pending_expert_mode"]=True     switch_expert_mode
/default             _cmd_default   state["pending_expert_mode"]=False    switch_expert_mode
/report [topic]      _cmd_report    state["pending_report"]=arg           _trigger_report
/exit /quit          _cmd_exit      return True                           退出
/expand              _cmd_expand    renderer.expand_last_round()          无
/thinking on|off     _cmd_thinking  renderer.state["thinking_enabled"]     无
/tools               _cmd_tools     get_available_tools 列表              无
/model [p [m]]       _cmd_model     无参显示 / 有参设 pending_model_switch switch_model
/thread              _cmd_thread    thread_id / thread_dir / 最近 run     无
/prompt ...          _cmd_prompt    list / show / reload                  无
/skill ...           _cmd_skill     list/search/override/evolution/install 无
/mcp list|reload     _cmd_mcp       list 显示 / reload 设 pending_mcp_reload reload_mcp_tools
──────────────────────────────────────────────────────────────────────────────────────
说明：需要主循环动作的命令只设 state 里的 pending_*，由 main 消费执行（解耦）
```

---

### 0.3. 运行时调用树

```
① 主循环分发（main._run_chat_async）
main._run_chat_async
    └─ prompt.startswith("/")
          └─ handle_command(prompt, console, renderer, cli_state, runtime)
                ├─ parts = prompt.split(maxsplit=1) → name / arg
                ├─ spec = _registry.get(name)
                │     └─ None → console.print("Unknown command") → return False
                ├─ ctx = CommandContext(console, renderer, state, runtime, arg)
                └─ result = spec.handler(ctx)
                      │
                      ├─ _cmd_help      → _registry.list_all() → console.print
                      ├─ _cmd_expert    → state["pending_expert_mode"]=True
                      ├─ _cmd_report    → state["pending_report"]=arg
                      ├─ _cmd_exit      → return True（should_exit）
                      ├─ _cmd_expand    → renderer.expand_last_round()
                      │     └─ stream_handler 展开 Thought + 工具结果
                      ├─ _cmd_thinking  → renderer.state["thinking_enabled"] 切换
                      ├─ _cmd_model     → 无参显示 / 有参设 pending_model_switch
                      ├─ _cmd_thread    → 读 thread_journal.events_path
                      ├─ _cmd_prompt    → get_prompt_manager() 操作
                      ├─ _cmd_skill     → runtime.skill_manager 操作
                      │     ├─ list / search / override / enable / disable
                      │     ├─ evolve / capture / history / health / eval-history
                      │     └─ install（本地 parser.install / remote hub Installer）
                      └─ _cmd_mcp       → runtime.mcp_manager 操作

② 主循环消费 pending（下一轮同轮消费）
main._run_chat_async
    ├─ pending_expert_mode → runtime.switch_expert_mode
    ├─ pending_report      → _trigger_report
    ├─ pending_mcp_reload  → runtime.reload_mcp_tools
    └─ pending_model_switch→ runtime.switch_model

③ 补全器绑定
main._run_chat_async
    └─ SlashCommandCompleter(get_registry(), skill_provider=...)
          └─ 用 CommandSpec.description 作 display_meta（与 /help 共用）
```

---

### 0.4. 补充

**`commands.py` 是 CLI 命令系统——`handle_command` 从 `_registry` 查 handler 分发，各命令通过 `CommandContext` 统一签名；需要主循环动作的命令只设 `state` 里的 `pending_*`，由 `main` 消费执行。**

几条主线：

- **统一签名 + 泛化分发**：所有 `_cmd_*` 接收 `CommandContext`，`handle_command` 无需为每条命令写分支——`CommandRegistry` 查表 + 统一调用。
- **pending 标志解耦**：`/expert` `/default` `/report` `/model` `/mcp reload` 只**设置 `state` 里的 `pending_*`**，由 `main` 主循环检测后执行实际动作——**避免 `commands.py` 依赖 `main.py`**。
- **`pending_report` 哨兵区分**：`""` 表"pending 无 topic"，`None` 表"未设 pending"——避免空字符串与"未设置"混淆。
- **描述单一来源**：`/help` 文案与补全菜单 `display_meta` **共用 `CommandSpec.description`**——避免两处维护。
- **两大命令族**：`/skill`（list / search / override / enable / disable / evolve / capture / history / health / eval-history / install）与 `/mcp`（list / reload）——`/skill` 是命令数最多的族，覆盖技能全生命周期。
- **`/skill install` 双路径**：本地路径用 `parser.install`；remote identifier（`github:` / `well-known:` / `claude-marketplace:` / `builtin:`）用 hub `Installer`（多 source）。
- **错误容错广泛**：几乎每个命令都有 `try/except`，失败打印红字而不中断主循环。
- **`_cmd_exit` 是唯一返回 True 的命令**：`/exit` 与 `/quit` 共用同一 handler。
- **注册表模块级唯一**：`_registry` 在模块加载时一次性注册 15 条命令；`get_registry()` 供 `main` 构造补全器使用——**`/help` 与补全共用同一注册表**。
- **与 `stream_handler` 的耦合点**：`/expand` `/thinking` 直接操作 `renderer.state`——命令系统与渲染器通过 `CommandContext.renderer` 衔接。
- **与 `runtime` 的耦合点**：`/model` `/thread` `/skill` `/mcp` 读 `runtime` 的 `capability_registry` / `thread_id` / `skill_manager` / `mcp_manager`——命令系统是 runtime 能力的对外交互面。