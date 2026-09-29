# 功能定义：会话以文件夹为基础（thread 绑定 cwd）

## 1. 功能一句话

**thread（会话）绑定一个工作目录（cwd），会话的"文件访问 / 上下文引用 / 会话切换"都以该目录为基准，并支持多目录引用与同目录会话引用。**

---

## 2. 要解决的核心问题

| 问题 | 现状 | 目标 |
|---|---|---|
| **多项目隔离** | 所有 thread 共用一个 CWD | 每个 thread 绑定自己的 cwd |
| **文件引用** | 无 `@文件` 语法 | 支持 `@文件` 塞进上下文 |
| **同目录会话引用** | 无 | 支持 `@thread-id` 引用同目录的其他会话 |
| **多目录引用** | 无 | `/add-dir` 追加引用目录 |
| **工作目录切换** | 无 | `/cd` 切目录 + 新会话绑新目录 |
| **默认会话绑定** | 无 | 进入 poirot 时默认会话绑 cwd |

---

## 3. 功能拆解（5 条子功能）

### 子功能 1：**文件访问 + `@文件` 引用**

- **基础能力**：**会话只能访问 `cwd`（+ 追加目录）下的文件**；
- **CLI 语法**：**`@<相对路径>`** —— **把文件内容塞进本轮上下文**；
- **示例**：`@src/main.py 帮我看看这个文件`；
- **约束**：
  - **路径必须在 `cwd`（或 `/add-dir` 追加目录）下** —— 否则拒绝；
  - **文件大小限制**（如 100KB）—— 防爆上下文；
  - **二进制文件** —— 拒绝 / 提示。

### 子功能 2：**同目录会话引用 `@thread-id`**

- **能力**：**引用"同一 cwd 下其他 thread"的对话历史**（user/assistant 消息）；
- **CLI 语法**：**`@thread-<id>`** 或 **`@<thread-id>`** 或者thread自动总结的会话名 —— 把该 thread 的历史注入本轮上下文；
- **约束**：
  - **被引用的 thread 必须在同一 cwd 下**（**和 Codex 的"同目录会话"一致**）；
  - **只注入 user / assistant 消息**（**不含 tool / system**）；
  - **截断 / 摘要**（**防历史过长**）—— 如"最近 N 轮 / 最多 X token"。

### 子功能 3：**默认会话绑 cwd**

- **能力**：**在某个文件夹下启动 poirot** —— **默认会话的 cwd = 该文件夹**；
- **行为**：
  - **`cd /path/to/project-a && poirot`** —— 默认 thread 的 `cwd = /path/to/project-a`；
  - **默认 `thread_id`**：`default-thread`（或 UUID）—— **但 `cwd` = 当前目录**。

### 子功能 4：**`/add-dir <目录>` 追加引用目录**

- **能力**：**在当前会话中追加一个"可引用目录"**；
- **CLI 语法**：**`/add-dir /path/to/lib`**；
- **效果**：
  - 之后的 `@<文件>` —— **可以引用 `/path/to/lib` 下的文件**；
  - **`@<文件>` 解析**：**先在 `cwd` 找；找不到** —— **再在追加目录找**；
- **约束**：
  - **追加目录可多个**；
  - **会话级**（**不跨会话**）；
  - **`/add-dir --remove <目录>`** —— 移除。

### 子功能 5：**`/cd <目录>` 切换工作目录**

- **能力**：**切换当前会话的工作目录** —— **新会话绑新目录**；
- **CLI 语法**：**`/cd /path/to/project-b`**；
- **语义**（**关键**）：
  - **`/cd` = "换 cwd + 开新会话"**；
  - **旧会话**：**保留**（**在旧目录下**）；
  - **新会话**：**新 `thread_id`** —— **`cwd` = 新目录**；
  - **current thread** —— **切到新会话**；
  - **`/thread switch`** —— **切回旧会话**（**如果切回旧目录**）；
- **约束**：
  - **新目录必须存在**；
  - **沙箱根** —— **跟着 `cwd` 变**（**重建 runtime / sandbox**）。

---


## 4. 涉及模块

| 子功能 | 主要模块 | 说明 |
|---|---|---|
| **1. 文件访问 + `@文件`** | `sandbox`（`PathMapping` / `SecurityGuard`）+ `cli`（解析 `@`） | 沙箱根 = cwd；CLI 解析 `@` → 读文件 → 注入 |
| **2. `@thread-id`** | `cli`（解析 `@`）+ `runtime`（读 thread 历史） | 解析 `@thread-xxx` → 读该 thread 的 messages → 注入 |
| **3. 默认会话绑 cwd** | `bootstrap`（`project_dir`）+ `run_manager`（记录 cwd） | 启动时 `cwd = Path.cwd()`；thread 元数据存 cwd |
| **4. `/add-dir`** | `cli`（命令）+ `sandbox`（多 `PathMapping`）+ 会话状态 | 追加 `PathMapping`；会话级保存 |
| **5. `/cd`** | `cli`（命令）+ `bootstrap`（重建 runtime） | 重建 runtime / sandbox；新 `thread_id` |

---

## 5. 关键设计决策

| 决策 | 选项 | 建议 |
|---|---|---|
| **cwd 来源** | `Path.cwd()` / `--project` 参数 | **`Path.cwd()`**（简单） |
| **thread_id** | `default-thread` / UUID | **UUID**（避免冲突） |
| **`@文件` 语法** | `@file.py` / `@./file.py` | **`@<相对路径>`**（**相对 cwd**） |
| **`@thread-id` 语法** | `@thread-xxx` / `@<uuid>` | **`@thread-<id>`**（**前缀区分**） |
| **`/add-dir` 作用域** | 会话级 / 全局 | **会话级**（**不跨会话**） |
| **`/cd` 语义** | 换 cwd / 换 cwd + 新会话 | **换 cwd + 新会话**（**旧会话保留**） |
| **多目录引用顺序** | cwd 优先 / 追加目录优先 | **cwd 优先**（**主目录）** |
| **会话历史截断** | 最近 N 轮 / token 上限 | **token 上限 + 摘要** |

---

## 6. 分阶段实施

### P0：**thread 绑 cwd + 默认会话绑 cwd**（最小可用）

- **`bootstrap_runtime` 接受 `project_dir`**（默认 `Path.cwd()`）；
- **`AppRuntime` 持 `project_dir`**；
- **`thread` 元数据存 `cwd`**（注册表 / `RunRecord.metadata`）；
- **sandbox 根 = `project_dir`**（`PathMapping(host_prefix=project_dir)`）。

**验证**：`cd /path/a && poirot` —— **沙箱根 = `/path/a`** —— **工具限制在它下**。

### P1：**`@文件` 引用**

- **CLI 解析 `@<path>`** —— 读文件 → 注入到本轮消息；
- **路径校验**：必须在 `cwd` 下。

**验证**：`@src/main.py 帮我看看` —— **文件内容进上下文**。

### P2：**`/add-dir`**

- **CLI `/add-dir <path>`** —— 追加 `PathMapping`；
- **`@文件` 解析** —— **先 cwd，再追加目录**。

**验证**：`/add-dir /lib` —— `@lib/foo.py` 能引用。

### P3：**`@thread-id` 引用**

- **CLI 解析 `@thread-<id>`** —— 读该 thread 的 messages（**user/assistant**）→ 注入；
- **校验**：被引用的 thread 必须在**同一 cwd** 下。

**验证**：`@thread-abc 那个会话说了什么` —— 注入历史。

### P4：**`/cd` 切目录**

- **CLI `/cd <path>`** —— 重建 runtime（**新 cwd**）—— **新 `thread_id`**；
- **旧会话保留**（**旧目录下**）。

**验证**：`/cd /path/b` —— 新会话绑 `/path/b` —— `/thread switch <旧>` 能切回。

### P5（可选）：**会话持久化**（跨进程恢复）

- **`checkpointer` 换 `SqliteSaver`**；
- **会话注册表 + 列表**。

**验证**：重启后 —— 同 `thread_id` —— **能继续**。

---

## 7. 边界与约束

| 边界 | 约束 |
|---|---|
| **文件路径** | 必须在 `cwd`（或追加目录）下；不允许 `../` 穿越 |
| **文件大小** | 单文件 ≤ 100KB（可配）；超限提示 |
| **文件类型** | 二进制 / 图片 —— 拒绝或特殊处理 |
| **`@thread-id`** | 必须同 `cwd`；只注入 user/assistant；历史截断 |
| **`/add-dir`** | 目录必须存在；会话级；可多目录 |
| **`/cd`** | 目录必须存在；旧会话保留；新会话新 `thread_id` |
| **沙箱** | 根 = `cwd` + 追加目录；越界拒绝 |


