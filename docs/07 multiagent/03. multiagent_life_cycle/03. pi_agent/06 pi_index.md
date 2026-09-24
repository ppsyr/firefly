---

name: pi-sandbox-bridge — Pi extension：转发 8 个工具到 Poirot SpecialistMcpServer。

description: 

【整体职责】
作为 pi CLI 的 extension，注册 8 个 poirot_* 工具，全部通过 stdio MCP 协议转发到
Poirot SpecialistMcpServer。配合 `pi --no-builtin-tools` 使用，禁用 pi 自带工具，
强制所有文件操作走 Poirot 沙箱（经 PathTranslator + SecurityGuard）。

【内容摘要】
- POIROT_TOOLS          : 8 个工具的 name + description + JSON Schema（bash / read_file /
                          write_file / list_dir / str_replace / glob / grep / download_file）。
- default export        : extension 入口，遍历 POIROT_TOOLS，逐个 pi.registerTool。
- callPoirotMcp()       : 通过 stdio MCP 协议调 Poirot SpecialistMcpServer。

【职责边界】
- 只负责：注册工具、转发调用、解析 MCP 响应。
- 不负责：沙箱路径翻译（Poirot SpecialistMcpServer 负责）、安全校验（同上）、pi 主流程（pi 自己负责）。
- 不持有状态：每次调用启动新子进程（MVP）。

---

## 0. 结构树

### 0.1. 静态结构树

```
index.ts — pi-sandbox-bridge（Pi extension：转发 8 工具到 Poirot SpecialistMcpServer）
│
├─ POIROT_TOOLS: Array<{name, description, parameters}>   ← 8 个工具定义
│   │   用途：声明 pi agent 可见的 8 个工具的名字、描述、参数 schema
│   │         pi 加载 extension 时读它，逐个注册
│   ├─ bash          ← { command }                用途：在沙箱执行 bash 命令
│   ├─ read_file     ← { path }                   用途：读沙箱内文件
│   ├─ write_file    ← { path, content }          用途：写文件到沙箱
│   ├─ list_dir      ← { path }                   用途：列目录内容
│   ├─ str_replace   ← { path, old_str, new_str } 用途：文件内字符串替换
│   ├─ glob          ← { pattern, path? }         用途：按 glob 模式找文件
│   ├─ grep          ← { pattern, path }          用途：按正则搜文件内容
│   └─ download_file ← { url, path }              用途：下载文件到沙箱
│   （全部用 plain JSON Schema object，不依赖 typebox）
│
├─ default export function (pi)              ← ★ Extension 入口
│   │   用途：pi 加载 extension 时调一次；遍历 POIROT_TOOLS，逐个注册工具
│   │         每个工具的执行体统一转发给 callPoirotMcp
│   │
│   └─ for toolDef of POIROT_TOOLS:
│       └─ pi.registerTool({
│             name: `poirot_${toolName}`,    ← 用途：加 poirot_ 前缀防冲突
│             label: `Poirot ${toolName}`,   ← 用途：UI 显示名
│             description,                   ← 用途：给 LLM 看的工具说明
│             parameters,                    ← 用途：参数 schema（LLM 填参依据）
│             execute: async (_toolCallId, params) => {
│               │   用途：工具被调用时的执行体
│               └─ callPoirotMcp(toolName, params)
│                   用途：把调用转发到 Poirot MCP server
│             },
│           })
│
└─ callPoirotMcp(tool, args): Promise<string> ← ★ stdio MCP 调用实现
    │   用途：启动 SpecialistMcpServer 子进程，通过 JSON-RPC 转发一次工具调用
    │         解析响应，返回文本结果
    │
    ├─ ① 读 env POIROT_SANDBOX_MCP_ENDPOINT
    │     用途：拿到 SpecialistMcpServer 的启动命令
    │     └─ 未设置 → throw Error（说明 PiRuntime 没注入）
    │
    ├─ ② endpoint.split(" ") → cmdParts
    │     用途：把命令字符串拆成 [command, ...args]
    │
    ├─ ③ spawn(cmdParts[0], cmdParts.slice(1), stdio=["pipe","pipe","pipe"])
    │     用途：启动 SpecialistMcpServer 子进程，建立三条管道
    │
    ├─ ④ 注册事件监听：
    │     ├─ proc.stdout.on("data")  → stdoutBuffer += ...
    │     │     用途：累积子进程标准输出（MCP 响应）
    │     ├─ proc.stderr.on("data")  → stderrBuffer += ...
    │     │     用途：累积标准错误（出错时报告用）
    │     ├─ proc.on("error")        → reject(spawn failed)
    │     │     用途：子进程启动失败时拒绝 Promise
    │     └─ proc.on("close", code)  → 解析 stdoutBuffer
    │           用途：子进程退出时处理结果
    │           ├─ code != 0 → reject(exited with code)
    │           │     用途：非零退出视为失败
    │           └─ code == 0 → 逐行 JSON.parse：
    │                 ├─ msg.result.content → 提取 text → resolve
    │                 │     用途：成功响应，返回工具结果文本
    │                 ├─ msg.error → reject(MCP error)
    │                 │     用途：MCP 层错误，拒绝 Promise
    │                 └─ 非 JSON → continue
    │                       用途：跳过日志行等非 JSON 内容
    │                 └─ 无有效 response → resolve(stdoutBuffer)
    │                       用途：兜底，返回原始 stdout
    │
    └─ ⑤ 发两条 JSON-RPC 到 stdin：
          ├─ initialize（id=0）              用途：MCP 握手（协议要求先 initialize）
          └─ tools/call（id=1, name, args）  用途：实际的工具调用请求
          └─ stdin.end()                     用途：关闭输入，通知子进程请求结束
```

---

### 0.2. 运行时调用树

```
① pi 子进程启动（由 PiRuntime 启动）
PiRuntime._build_command
    │   用途：组装 pi 命令，加 --no-builtin-tools + -e <本文件>
    └─ pi --mode rpc --no-session --no-builtin-tools -e <本文件路径>
        │
        ▼
pi 子进程启动
    ├─ --no-builtin-tools → 禁用 pi 自带 read/write/edit/bash
    │     用途：防止 agent 绕过 Poirot 沙箱直接操作文件
    ├─ -e <index.ts>      → jiti 加载本 extension
    │   └─ default export(pi) 被调用
    │       │   用途：注册 8 个工具到 pi
    │       └─ pi.registerTool × 8
    │           → agent 看到 poirot_bash / poirot_read_file / ... 8 个工具
    │
    └─ agent 决定调用某个 poirot_* 工具
        │
        ▼
② execute(_toolCallId, params)
    │   用途：工具执行体，收到 agent 传的参数
    └─ callPoirotMcp(toolName, params)
        │   用途：转发到 Poirot MCP server
        │
        ├─ ① 读 POIROT_SANDBOX_MCP_ENDPOINT
        │     （由 PiRuntime._build_env 注入）
        │     用途：拿到 SpecialistMcpServer 启动命令
        │
        ├─ ② spawn SpecialistMcpServer 子进程
        │     python -m ...specialist_mcp_server --sandbox-id <id>
        │     用途：启动独立的 Python MCP server 进程
        │
        ├─ ③ 写两条 JSON-RPC：
        │     {"jsonrpc":"2.0","id":0,"method":"initialize",...}
        │     用途：MCP 协议握手（必须先 initialize）
        │     {"jsonrpc":"2.0","id":1,"method":"tools/call",...}
        │     用途：真正的工具调用请求
        │
        └─ ④ 解析 stdout 的 JSON-RPC response
            │   用途：从子进程输出里提取工具结果
            ▼
        Poirot SpecialistMcpServer
            ├─ PathTranslator 翻译路径    用途：虚拟路径 ↔ 宿主路径映射
            ├─ SecurityGuard 安全校验      用途：拦截危险命令/路径
            └─ 调 sandbox 执行             用途：真正跑命令/读写文件
                │
                ▼
            返回 { result: { content: [{ type: "text", text: "..." }] } }
        │
        ▼
    resolve(text) → 包成 pi 的 tool result → 回给 agent
```

---

## 1. 这个文件是干什么的

**一句话：它是 pi CLI 的扩展（extension），把 8 个 `poirot_*` 工具注册进 pi，让 pi agent 能通过 stdio MCP 协议调用 Poirot 沙箱。**

### 1.1. 三件事

| 做的事 | 具体 | 对应代码 |
|---|---|---|
| **注册工具** | 定义 8 个工具，加 `poirot_` 前缀后注册进 pi | `POIROT_TOOLS` + `default export` |
| **转发调用** | agent 调 `poirot_bash` → extension 执行 `callPoirotMcp("bash", args)` | `execute` 回调 |
| **桥接 MCP** | 启动 Poirot SpecialistMcpServer 子进程，通过 stdio JSON-RPC 转发请求、收结果 | `callPoirotMcp` |

### 1.2. 它解决的三个问题

**问题 ①：pi 自带工具绕过 Poirot 沙箱**

pi 默认有 `read` / `write` / `edit` / `bash` 等内置工具，直接操作文件系统——**不经过 Poirot 的路径翻译和安全校验**。

解法：PiRuntime 启动 pi 时加 `--no-builtin-tools` 禁掉它们，再用本 extension 注册 `poirot_*` 替代。

**问题 ②：pi 和 Poirot 之间需要协议桥接**

pi 的 extension API 是 JS/TS 的，Poirot 的沙箱能力是 Python 的。**两者跨语言、跨进程。**

解法：用 MCP 协议（stdio JSON-RPC）做桥——extension 侧 spawn 一个 Python 子进程，通过 stdin/stdout 交换 JSON。

**问题 ③：8 个工具要统一入口**

不希望 pi 直接调 sandbox 的 8 个底层接口——**希望所有调用都过 Poirot SpecialistMcpServer**。

解法：extension 只声明工具 schema，实际执行全部转发给 MCP server。

### 1.3. 它在链路里的位置

```
PiRuntime（Python）
    │ 注入 POIROT_SANDBOX_MCP_ENDPOINT env
    │ 用 -e 加载本 extension
    ▼
pi 子进程（Node.js）
    │ jiti 加载 index.ts
    │ registerTool × 8
    ▼
pi agent 调用 poirot_bash
    │
    ▼
本文件 callPoirotMcp（TypeScript）
    │ spawn + stdio JSON-RPC
    ▼
SpecialistMcpServer（Python）
    │ PathTranslator + SecurityGuard
    ▼
sandbox 执行
```

**本文件是"pi 世界"和"Poirot 世界"之间的桥。**

---

## 2. 为什么用 TS

### 2.1. 因为 pi 的 extension API 是 TS/JS 的

**关键约束：pi 的 extension 机制只接受 JS/TS 模块。**

看 extension 入口：

```typescript
export default function (pi) {
  pi.registerTool({ ... });
}
```

`export default` 是 ES Module 语法——**这是 JS/TS 世界的约定**。pi 加载 extension 时，期望拿到一个"导出默认函数的模块"。

**Poirot 想要扩展 pi 的能力，就必须用 pi 接受的语言写。** 这不是选择，是约束。

### 2.2. 因为要 spawn 子进程 + 处理 stdio

extension 需要：

- `spawn` 子进程（`require("child_process")`）
- 处理 stdio 流（`proc.stdout.on("data", ...)`）
- 发 JSON-RPC（`JSON.stringify` + `stdin.write`）

**这些都是 Node.js 的原生能力**——用 TS/JS 写最自然。

### 2.3. 为什么不是纯 JS 而是 TS

**因为 TS 提供了类型表达力**（虽然本文件用了 `// @ts-nocheck` 跳过检查）。

```typescript
const POIROT_TOOLS = [
  {
    name: "bash",
    parameters: {
      type: "object",
      properties: {
        command: { type: "string", description: "..." },
      },
      required: ["command"],
    },
  },
  ...
];
```

**TS 的接口 / 类型声明让工具 schema 更清晰**——即便运行时跳过检查，源码可读性更好。

### 2.4. 为什么加 `// @ts-nocheck`

注释里写得很清楚：

> **pi 用 jiti 加载，跳过 tsc 类型检查；Poirot 仓库无 npm 依赖**

两个原因：

1. **jiti 是运行时加载器**：它直接执行 TS，不做类型检查。所以 `@ts-nocheck` 不影响运行时。
2. **Poirot 仓库没有 npm 依赖**：本文件在 Python 仓库里，没有 `node_modules`，`import` 的包（如 typebox）装不上。所以干脆**用 plain JSON Schema object，不依赖任何 npm 包**。

### 2.5. 与 Python 侧的对比

| 维度 | Poirot（Python） | pi-sandbox-bridge（TS） |
|---|---|---|
| 语言 | Python | TypeScript |
| 运行环境 | Python 解释器 | Node.js（jiti 加载） |
| 加载方式 | `import` | jiti 运行时加载 |
| 依赖管理 | pyproject.toml | 无 npm 依赖 |
| 通信方式 | — | stdio JSON-RPC |
| 为什么这么写 | Poirot 主项目 | **pi 只接受 JS/TS extension** |

### 2.6. 为什么不用 Python 写 extension

**因为 pi 不认识 Python extension。**

pi 的 extension 机制设计成"加载 JS/TS 模块"——**如果 pi 支持 Python extension，Poirot 也可以直接用 Python 写**。但现实是 pi 只认 JS/TS，所以 Poirot 只能：

- **用 TS 写 extension（本文件）**
- **用 Python 写 MCP server（SpecialistMcpServer）**
- **两者通过 stdio JSON-RPC 通信**

**这是一种"被迫的跨语言桥接"**——不是 Poirot 想用 TS，而是 pi 的扩展机制要求用 TS。

---
## 3. Pi 子进程是在 CLI 中直接启动的吗？能直接执行 TS 文件吗？

**是的，PiRuntime 直接 `subprocess.Popen` 启动 `pi` 命令；但 pi 不是"直接执行 TS"，而是 pi 自己用 jiti 加载 TS。**

这两件事需要分开看。

---

## 3.1. Pi 子进程怎么启动的

看 `PiRuntime._run_rpc_session`：

```python
proc = subprocess.Popen(
    cmd,                          # ["pi", "--mode", "rpc", "--no-session",
                                  #  "--no-builtin-tools", "-e", "<index.ts>", ...]
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    env=env,
)
```

**`subprocess.Popen` 直接在操作系统里启动 `pi` 这个可执行文件**——跟你在终端敲 `pi ...` 是一回事。

所以：

- **Python 侧**：只负责"启动 `pi` 进程 + 发 prompt 到 stdin + 读 stdout"
- **`pi` 是什么**：PATH 里的一个可执行命令（`shutil.which("pi")` 能找到）
- **不需要任何"CLI 层"中介**：直接 `Popen`，不走什么 `pi-cli` 之类的东西

**这跟 ClaudeCodeRuntime 启动 `claude` 是同一种方式**（都是 `subprocess` 直接起外部命令）。

---

## 3.2. `-e <index.ts>` 是什么意思

命令里有一段：

```python
cmd.extend(["-e", str(self._poirot_extension_path())])
```

`-e` 是 pi CLI 的参数，意思是"**加载这个 extension**"，值是：

```
<poirot包路径>/extensions/pi-sandbox-bridge/index.ts
```

**所以 pi 被启动后，它自己会去加载这个 `.ts` 文件。**

**关键点：`subprocess.Popen` 没有"执行 TS"的能力——它只是把 `-e <path>` 作为参数传给 `pi`。真正"加载 TS"的是 pi 自己。**

---

## 3.3. Pi 怎么"执行" TS 文件

**pi 不是"直接执行 TS"，而是用 jiti 加载。**

从注释里能看到：

> **jiti 跳过 tsc 类型检查**
> **pi 用 jiti 加载，跳过 tsc 类型检查；Poirot 仓库无 npm 依赖**

### jiti 是什么

**jiti 是一个 Node.js 的"运行时 TS/ESM 加载器"**——它能在运行时：

- 读 `.ts` 文件
- 剥离类型注解（不做类型检查）
- 转成 JS 执行

**不需要预先 `tsc` 编译，也不需要 `node_modules`。**

### 流程

```
pi 启动
  └─ 读 -e 参数 → <index.ts> 路径
      └─ jiti 加载该文件
          ├─ 剥离 TS 类型（如 `: string`）
          ├─ 转成 JS
          └─ 执行 → 拿到 default export 函数
              └─ 调用 default(pi)
                  └─ pi.registerTool × 8
```

**所以"执行 TS"这件事发生在 pi 进程内部，不在 Python 侧。**

### 为什么加 `// @ts-nocheck`

因为：

1. **jiti 不做类型检查**——它只是剥离类型，不验证类型对不对
2. **Poirot 仓库没有 npm 依赖**——`@sinclair/typebox` 之类的包装不上，所以干脆用 plain JSON Schema object
3. **`@ts-nocheck` 告诉工具"别检查这个文件"**——对 jiti 无影响，但方便人类读

---

## 3.4. 谁在"执行"TS

拆开责任：

| 环节 | 谁负责 | 做什么 |
|---|---|---|
| 启动 `pi` 进程 | **Python（PiRuntime）** | `subprocess.Popen(["pi", ...])` |
| 传 `-e <path>` 参数 | **Python** | 把 extension 路径作为参数 |
| 读 `-e` 参数 | **pi（Node.js）** | 解析命令行 |
| 加载 `.ts` 文件 | **pi + jiti** | jiti 剥类型、转 JS、执行 |
| 调 `default(pi)` | **pi** | 触发 extension 入口 |
| 注册 8 个工具 | **extension** | `pi.registerTool` × 8 |
| 工具被调用时转发 | **extension** | `callPoirotMcp` → spawn Python MCP server |

**Python 侧只知道"启动 pi，传个 `-e` 参数"，不知道 pi 内部怎么处理 TS。**

---

## 3.5. 完整链路图

```
┌─────────────────────────────────────────────────────────┐
│ Python 进程（Poirot）                                     │
│                                                          │
│ PiRuntime._run_rpc_session                               │
│   └─ subprocess.Popen(                                   │
│        ["pi", "--mode", "rpc", "--no-session",           │
│         "--no-builtin-tools", "-e", "<index.ts>"],       │
│        ...)                                              │
└──────────────────────────┬──────────────────────────────┘
                           │ 操作系统级：fork/exec
                           ▼
┌─────────────────────────────────────────────────────────┐
│ pi 子进程（Node.js）                                      │
│                                                          │
│ ① 解析命令行：看到 -e <index.ts>                          │
│ ② jiti 加载 index.ts：                                   │
│    ├─ 剥离 TS 类型注解                                    │
│    ├─ 转成 JS                                             │
│    └─ 执行 → 拿到 default export                          │
│ ③ 调用 default(pi)：                                      │
│    └─ pi.registerTool × 8                                │
│ ④ agent 运行，调用 poirot_bash                            │
│    └─ extension 的 execute → callPoirotMcp               │
└──────────────────────────┬──────────────────────────────┘
                           │ spawn + stdio JSON-RPC
                           ▼
┌─────────────────────────────────────────────────────────┐
│ SpecialistMcpServer 子进程（Python）                      │
│   └─ PathTranslator + SecurityGuard → sandbox 执行        │
└─────────────────────────────────────────────────────────┘
```

---

## 3.6. 两个容易混淆的点

### 点 ①：`subprocess.Popen` 不能"执行 TS"

`subprocess.Popen` 只能启动**可执行文件**——它不知道什么是 TypeScript。

**它能启动 `pi`，但不能启动 `index.ts`。**

**真正"执行 TS"的是 pi 内部的 jiti。**

### 点 ②：`-e` 是 pi 的参数，不是 Python 的参数

```python
cmd.extend(["-e", str(self._poirot_extension_path())])
```

这行代码的意思是"**把 `-e <path>` 加到给 pi 的参数列表里**"。

**Python 自己不处理 `-e`**——它只是把它作为 argv 的一项传给 pi。pi 收到后才去解析。

---

## 3.7. 类比

| 角色 | 类比 |
|---|---|
| Python（PiRuntime） | 老板，喊一声"启动 pi，带上 `-e index.ts`" |
| `pi` 命令 | 员工，收到指令后自己去处理 |
| jiti | pi 员工的"翻译器"，把 TS 翻成能跑的 JS |
| `index.ts` | 一份用 TS 写的"工作手册" |
| Python（SpecialistMcpServer） | 另一个员工，被 extension spawn 起来干活 |

**老板（Python）不关心员工（pi）怎么读懂 TS——那是员工自己的事。**

---

## 3.8. 总结

> **Pi 子进程是 Python 用 `subprocess.Popen` 直接启动 `pi` 命令的（跟敲终端一样）。但 Python 不能"执行 TS"——它只是把 `-e <index.ts>` 作为参数传给 pi。真正加载并执行 TS 的是 pi 内部的 jiti 运行时加载器：它剥离类型注解、转成 JS、执行，然后触发 extension 的 `default(pi)` 入口。所以"执行 TS"发生在 pi 进程内部，Python 侧只负责启动 pi 和传参。**


## 4. 总结

> **这个文件是 pi CLI 的 extension，负责把 8 个 `poirot_*` 工具注册进 pi，并通过 stdio MCP 协议转发调用到 Poirot SpecialistMcpServer。用 TS 写不是选择，而是约束——pi 的 extension API 只接受 JS/TS 模块。由于 pi 不认 Python extension，Poirot 只能"TS 写桥 + Python 写 MCP server"，两者通过 stdio JSON-RPC 通信。这是跨语言桥接的典型方案。**