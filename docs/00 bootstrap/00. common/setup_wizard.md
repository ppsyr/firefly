---

name: 首次启动配置向导 — 检测 .env 不存在时引导用户完成最小配置。

description:

【整体职责】
在首次启动（.env 不存在）时，以交互式向导引导用户配置最小可运行项：选择 provider、
输入 API Key、选择可选功能（skill / MCP / sandbox），并从模板生成 .env 文件。
若检测到环境变量中已有任意 provider 的 API Key（Docker env_file 模式），跳过向导。

【内容摘要】
- _console                    : rich Console 单例。
- ensure_config               : 入口，检查 .env / env 注入，必要时启动向导。
- _has_any_provider_key       : 检查环境变量中是否已有 provider API Key。
- _run_wizard                 : 交互式向导主流程。
- _ask_yes_no                 : 是/否提问，回车取默认值。
- _ask_sandbox                : 沙箱模式选择，返回 provider 路径或空串。
- _build_env_content          : 从模板填充用户输入，生成 .env 内容。
- _MINIMAL_TEMPLATE           : .env.example 缺失时的最小模板。
- update_env_file             : 更新 .env 中指定 key（供 TUI 配置面板调用）。

【职责边界】
- 只负责：检测配置是否就绪、交互式采集配置、生成/更新 .env 文件。
- 不负责：配置的加载与校验（loader）、provider 的实现与模型构造（provider_config）、
  TUI 配置面板的呈现（tui 模块，仅调用 update_env_file）。

---

## 0. 结构树

### 0.1. 静态结构树

```
setup_wizard.py — 首次启动配置向导（.env 不存在时引导最小配置）
│
├─ _console: Console                          ← rich Console 单例
│
├─ _MINIMAL_TEMPLATE: str                     ← .env.example 缺失时的最小模板
│
├─ ensure_config(project_root) -> bool        ← 【入口】
│   ├─ .env 存在 → True
│   ├─ _has_any_provider_key() → True          ← Docker env_file 模式跳过
│   └─ _run_wizard(project_root, env_path)
│
├─ _has_any_provider_key() -> bool            ← 检查 env 中是否已有 provider key
│   └─ 遍历 PROVIDER_PROFILES（跳过 fake）→ env_key 非空
│
├─ _run_wizard(project_root, env_path) -> bool ← 交互式向导主流程
│   ├─ 打印欢迎 Panel（rich）
│   ├─ 【循环】选择 provider
│   │   ├─ 列出可选项（排除 fake，显示 ✓ / 默认标记）
│   │   ├─ 回车且 configs 为空 → 提示至少配一个，continue
│   │   ├─ 回车且有 configs → break
│   │   ├─ 编号非法 → 提示重试
│   │   ├─ 输入 API key（no_key_required 可为空）
│   │   ├─ 记录 configs[profile.name] = {api_key, base_url:"", model:""}
│   │   └─ 询问是否继续添加（y/N）
│   ├─ 可选功能询问
│   │   ├─ _ask_yes_no("启用 Skill 系统？")
│   │   ├─ _ask_yes_no("启用 MCP 工具？")
│   │   └─ _ask_sandbox()
│   ├─ _build_env_content(...) → content
│   ├─ env_path.write_text(content)
│   └─ 打印成功 / 取消提示
│   └─ KeyboardInterrupt / EOFError → False（取消）
│
├─ _ask_yes_no(prompt, default=False) -> bool ← 是/否提问
│   └─ 空输入取 default；"y"/"yes" 为 True
│
├─ _ask_sandbox() -> str                      ← 沙箱选择
│   ├─ "1" → LocalSandboxProvider 路径
│   ├─ "2" → DockerSandboxProvider 路径
│   └─ 其他/回车 → ""
│
├─ _build_env_content(project_root, configs, skill, mcp, sandbox) -> str
│   ├─ 读 .env.example（缺失 → _MINIMAL_TEMPLATE）
│   ├─ 构造 overrides：
│   │   ├─ {PROVIDER}_API_KEY（base_url/model 空则保留模板默认）
│   │   ├─ POIROT_SKILL_ENABLED
│   │   ├─ POIROT_MCP_ENABLED
│   │   └─ POIROT_SANDBOX_USE（sandbox 非空时）
│   └─ 逐行替换 KEY= 行（注释与其他行原样保留）
│
└─ update_env_file(env_path, overrides) -> None ← 更新已有 .env（供 TUI 配置面板）
    ├─ .env 不存在 → return
    └─ 逐行替换 KEY= 行 → 写回
```

---

### 0.2. 运行时调用树

```
① CLI 启动（main 最开始）
main.main
    └─ from setup_wizard import ensure_config
          └─ ensure_config(_PROJECT_ROOT)
                ├─ .env 存在 → True → main 继续
                ├─ _has_any_provider_key()
                │     └─ 遍历 PROVIDER_PROFILES，跳过 fake，env_key 非空 → True
                │           （Docker env_file 模式，跳过向导）
                └─ _run_wizard(_PROJECT_ROOT, env_path)
                      │
                      ├─ Panel 欢迎
                      ├─ 【provider 选择循环】
                      │     ├─ 列出 selectable（排除 fake）
                      │     ├─ input 编号 → profile
                      │     ├─ input API key
                      │     ├─ configs[profile.name] = {...}
                      │     └─ 继续？(y/N)
                      ├─ _ask_yes_no × 2（skill / mcp）
                      ├─ _ask_sandbox()
                      ├─ _build_env_content(...)
                      │     ├─ 读 .env.example（或 _MINIMAL_TEMPLATE）
                      │     ├─ 构造 overrides
                      │     └─ 逐行替换 → content
                      ├─ env_path.write_text(content)
                      └─ 返回 True
                └─ False（取消）→ main return 1

② main 重新加载 .env
main.main
    └─ load_dotenv(_PROJECT_ROOT / ".env", override=True)
          └─ 使向导刚生成的配置生效

③ TUI 配置面板（Ctrl+B）
app.tui.settings_screen
    └─ update_env_file(env_path, overrides)
          ├─ 读当前 .env
          ├─ 逐行替换 KEY= 行
          └─ 写回
```

---

### 0.3. 补充

**`setup_wizard.py` 是首次启动的配置引导——检测 `.env` 不存在时，交互式采集 provider key 与可选功能开关，从模板生成 `.env`；同时提供 `update_env_file` 供 TUI 配置面板更新已有 `.env`。**

几条主线：

- **接口小**：对外仅 `ensure_config`（一个入口）与 `update_env_file`（TUI 用）——符合注释里"接口小"的设计原则。
- **三条"已就绪"路径**：① `.env` 存在；② Docker env_file 模式（env 已注入 provider key）；③ 用户走完向导生成 `.env`。任一成立即返回 True。
- **向导不依赖 .env**：因为运行前 `.env` 不存在——除 `_has_any_provider_key` 检查**已注入的环境变量**外，不做 env 判断。
- **零新依赖**：仅用 rich + 内置 `input()`，不引入 click / questionary 等。
- **模板驱动**：生成的 `.env` 从 `.env.example` 逐行替换，保证字段完整；`.env.example` 缺失时回退 `_MINIMAL_TEMPLATE`。
- **逐行替换策略**：只替换 `KEY=` 行，**注释与其他行原样保留**——`_build_env_content` 与 `update_env_file` 共用同一套替换逻辑（两份实现，逻辑一致）。
- **"至少一个 provider"约束**：空回车时若 `configs` 为空则不允许退出，强制至少配置一个。
- **延迟导入 `PROVIDER_PROFILES`**：避免在已有 `.env` 时无谓加载 provider 配置。
- **与 `main.py` 的衔接**：`main` 开头先 `ensure_config`，失败返回 1；成功后 `load_dotenv(..., override=True)` **重新加载**——这是向导生成配置后立即生效的关键一步。
- **与 TUI 的衔接**：`update_env_file` 专供 TUI 设置面板（Ctrl+B）调用，是向导之外的第二条配置修改入口。
- **取消可退**：`Ctrl+C` / `EOFError` 捕获后返回 False，不写文件——保证半成品不落地。