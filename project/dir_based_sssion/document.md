# dir based session 功能完成情况

## 1. thread 持久化与恢复

### 已完成功能

- **跨进程恢复会话**：LangGraph 的完整 thread checkpoint 已持久化到用户目录。退出并重新启动后，可选择原会话继续对话；不同会话的消息和状态相互隔离。
- **会话元数据与标题**：新会话立即保存 ID、标题、创建时间和更新时间。首次用户消息自动生成标题，不调用 LLM；用户可手动重命名，后续对话不会覆盖手动标题。
- **会话管理**：实现 `/thread`、`info`、`list`、`new`、`switch <id>`、`rename <title>` 和 `delete <id>`，并提供子命令补全。删除当前会话前须先切换或新建会话。
- **CLI 选择器**：`/thread list` 在下栏显示最近会话，支持方向键滚动、回车恢复和 Esc 取消；非交互终端会列出完整 ID，供 `switch` 使用。TUI 继续使用同一套 runtime 和持久化状态。
- **按会话归档**：元数据、checkpoint、会话事件和每次运行的日志放在同一个按创建日期分层的会话目录。恢复或重命名不会移动目录。

### 存储布局

默认根目录取当前运行用户的主目录 `Path.home() / ".poirot"`，不包含写死的用户名；在本机展开为 `/Users/lucia/.poirot`。设置 `POIROT_STORAGE_ROOT` 可覆盖根目录，测试和部署时也可通过配置覆盖 `storage_root`。

```text
~/.poirot/
└── sessions/
    └── {year}/{month}/{day}/
        └── thread-{HH-MM-SS}-{thread_id}/
            ├── metadata.json
            ├── checkpoints.db
            ├── thread-events.jsonl
            └── runs/
                └── {run_id}/
                    ├── record.json
                    └── events.jsonl
```

`year/month/day` 和 `HH-MM-SS` 使用会话**创建时的本地时间**，目录名中的 `thread_id` 是完整 ID。`metadata.json` 保存 `thread_id`、`title`、`created_at`、`updated_at` 和 `title_set`；时间字段使用带时区的 ISO 格式。每个会话的 `checkpoints.db` 保存该会话的 LangGraph checkpoints 与 pending writes。`runs/` 中还可能包含运行过程中生成的报告、产物和其他文件。

### 实现方式

- `ThreadStore` 按 `thread_id` 定位会话目录，创建时写入 `metadata.json`，列出时按 `updated_at` 降序排序。元数据写入使用同目录临时文件、`fsync` 和原子替换；无效元数据会发出警告，不会被自动删除。
- `SessionCheckpointer` 按会话把同步与异步的 LangGraph checkpoint 调用路由到对应目录的 `checkpoints.db`。底层 `SQLiteCheckpointer` 使用 `AsyncSqliteSaver`；连接由应用运行时持有并在关闭时释放。
- 切换会话前先校验元数据并读取目标 checkpoint，读取失败时保持当前 runtime；切换成功后沿用目标会话目录。新会话立即创建元数据，首次用户消息更新标题；手动重命名由 `title_set` 保护。
- 删除时先清除目标会话的 checkpoints 和 pending writes，再删除元数据。当前会话不能删除；会话目录中的日志、报告、产物以及长期记忆不随删除操作清理。

### 旧数据迁移

- 读取旧版 `~/.poirot/threads/{thread_id}.json` 时，`ThreadStore` 保留原有 ID、标题和时间，按原创建时间写入新目录的 `metadata.json`，然后移除旧元数据文件。
- 首次打开该会话的 checkpoint 时，从旧版共享 `~/.poirot/checkpoints.db` 复制该 `thread_id` 的 checkpoints 和 pending writes 到会话目录。旧共享数据库保留，以便其他尚未迁移的会话继续读取。
- 迁移是按会话、按需进行的：仅列出会话会迁移元数据；恢复或使用该会话时才迁移 checkpoint。旧版位于项目日志目录的历史日志不会自动搬入新目录。

### 验证

- 原有 thread 持久化实现曾通过 90 项功能和受影响入口测试；当时还实际验证了 CLI 重启后的选择、续聊、重命名、新建、删除以及 TUI 启动。该结果对应旧版共享数据库布局。
- 改为目录式存储后，受影响的 runtime、CLI、TUI 和集成测试 **68 项通过**；包含两个独立进程经实际应用装配入口恢复并续聊、默认路径随用户主目录变化、旧元数据与 pending writes 迁移、相似 ID 隔离及跨日期更新后目录不变。
- 目录式存储改动尚未运行全量测试，也未重新执行手工 CLI/TUI 流程；不能把旧版的手工验证或 90 项测试视为新布局的全量验证。

## 2. thread 绑定 dir

### 已完成功能

- 启动时可用 `--dir` 指定项目目录、用 `--project_name` 指定项目名；不传 `--dir` 时使用启动目录。目录会规范化为绝对路径，符号链接指向同一目录时复用项目，并校验目录和项目名冲突。
- 新 thread 的元数据立即写入 `project` 和 `cwd`。`/thread new` 使用当前项目；旧的未绑定 thread 仍能通过全局 `/thread` 恢复，不会被自动绑定。
- `/project list` 可选择项目，切换后创建该项目的新空 thread；`/project_thread list` 只列出并恢复当前项目的 thread。CLI 和 TUI 都支持上下键、回车、Esc，以及超过 10 项时继续滚动；非交互 CLI 直接打印列表。
- 项目元数据、项目 thread 索引和每次运行的 `project`、`cwd` 均已持久化。`--dir` 不改变进程当前目录或 sandbox 行为。

### 实现方式

- `runtime/projects.py` 负责项目注册、目录规范化与冲突校验，写入 `{storage_root}/projects/{project_name}.json`。
- `runtime/threads.py` 在原有 `{storage_root}/sessions/YYYY/MM/DD/thread-.../metadata.json` 中保存绑定信息，并在 thread 创建、更新、重命名和删除时同步 `{storage_root}/sessions/{project_name}/threads.json`。索引是派生数据，缺失或损坏时从 thread 元数据重建；原 session 目录和 checkpoint 布局不变。
- `app/bootstrap.py` 在启动时创建或加载项目，并由 `AppRuntime` 处理项目切换、项目内 thread 恢复及失败时保持原 runtime。CLI/TUI 的命令处理和选择器负责交互；`RunRecord` 保存本次运行的项目绑定。

### 验证

- 功能及受影响的 runtime、CLI、TUI、thread 持久化测试共 **83 项通过**。跨进程测试验证了两个项目的 thread 在重启后可分别恢复并继续对话，checkpoint 和历史消息没有串用。
- 全量测试最近一次结果为 **2739 通过、4 跳过、13 失败**；失败项涉及既有配置断言、环境依赖及平台相关测试，未计入上述通过结论。

## 3. 文件访问权限（sandbox）

### 已完成功能

- **sandbox 按 thread 绑定目录隔离**：文件访问权限来源于当前 thread 元数据中的 `cwd`，不使用进程当前目录、项目名或项目索引推断。新建、恢复、切换 thread 或项目后，sandbox 随目标 thread 更新；切换失败时保留原 runtime 和原权限。旧的无 `cwd` thread 不会自动继承启动目录。
- **覆盖实际文件工具入口**：Local 和 Docker provider 均按 thread 创建或复用 sandbox；`bash`、`read_file`、`write_file`、`list_dir`、`str_replace`、`present_files` 等工具统一经过 `Sandbox` 的路径守卫、路径转换和 runtime 执行链路。下载、上传、glob、grep 等 facade 入口也执行同一边界检查。
- **路径越界与符号链接防护**：路径校验基于规范化后的真实路径和允许根目录判断，拒绝 `../`、越界绝对路径、相似目录名前缀以及指向允许范围外的符号链接；本地命令入口也会检查显式路径和重定向目标。
- **`@文件` / `@目录` 引用**：支持明确相对路径、绝对路径（仅在允许根内）和仅文件名搜索。单文件内容只注入当前轮；目录引用只注入相对路径和大小清单，不批量注入正文。
- **引用交互**：输入 `@` 后实时展示当前 thread 允许的候选；同名文件通过上下键、回车和 Esc 选择或取消，单命中也必须确认。确认后自动结束引用 token，用户是否手动输入空格都可以继续输入正文；非交互 CLI 输出候选，不擅自选择。
- **文件内容限制**：单文件最多 100 KiB；二进制、无法按 UTF-8 解码或读取失败的文件不注入。目录遍历有条数、总文本大小和访问目录数上限，超限会给出截断提示。
- **搜索性能优化**：候选索引按 thread 级文件访问对象懒加载并复用，后续输入只做内存前缀匹配；CLI 补全和 TUI 首次扫描在后台执行。候选发现和目录清单默认跳过 `.venv`、`venv`、`node_modules`、`.git`、`__pycache__`、`build`、`dist` 等依赖或构建目录。

### 实现方式

- `app/bootstrap.py` 从当前 thread 元数据读取 `cwd`，通过 `thread_cwd_resolver` 注入 `LocalSandboxProvider` 和 `DockerSandboxProvider`。Local provider 将 workspace mapping 的宿主路径替换为 thread `cwd`；没有 `cwd` 时不创建 workspace mapping，避免旧 thread 获得隐式目录权限。Docker provider 在 thread scope 下只允许 `/mnt/poirot/user-data/workspace` 及配置的允许挂载。
- `Sandbox` 固定执行 `guard.validate → translator.translate → runtime.execute → translator.mask`。`LocalSecurityGuard` 负责白名单、路径穿越、危险命令和绝对路径校验；`DockerPathGuard` 负责容器内 thread scope 和写入路径校验；`LocalRuntime` 对文件操作再次按真实路径检查允许根目录。
- `LocalRuntime` 的 `bash` 使用 `subprocess.run(..., shell=True)`。现有检查可以拦截显式越界路径、路径遍历和危险命令，但无法阻止命令在 shell、脚本或子进程中动态构造路径。因此 Local 模式的路径校验不是内核级进程隔离；需要更强进程边界时使用 Docker sandbox。
- `agents/runtime/file_access.py` 的 `ThreadFileAccess` 负责 `resolve`、`read_text`、目录清单、同名搜索和候选补全。所有入口都通过真实路径判断是否属于 `thread.cwd`；`@` 引用材料拼接到 enriched message，原始问题单独传给标题生成和运行记录。
- CLI 使用 `ThreadedCompleter`，TUI 使用带 generation 校验的 Textual worker；旧的异步扫描结果不会覆盖新输入。确认文件后写入带分隔符的 `@relative/path `，避免后续正文拼接到文件名后导致解析失败。
- 当前没有实现 `/add-dir`，因此实际权限范围只有当前 thread 的 `cwd`；`ThreadFileAccess` 保留 `extra_roots` 参数供未来会话级追加目录使用，但项目级索引不会作为权限来源。

### 验证

- 当前受影响的文件访问、CLI、TUI 和本地 sandbox 测试共 **85 项通过**，覆盖 thread cwd 隔离、越界路径、外部符号链接、文件类型和大小限制、目录清单、候选选择、确认后继续输入以及搜索索引和忽略目录。
- 受影响测试命令：

  ```bash
  PATH="$PWD/.venv/bin:$PATH" .venv/bin/pytest -q \
    poirot/backend/tests/v1/unit/runtime/test_file_access.py \
    poirot/backend/tests/v1/unit/cli \
    poirot/backend/tests/v1/unit/tui \
    poirot/backend/tests/v1/unit/sandbox/test_local_runtime.py
  ```

- 最近一次全量测试结果为 **2751 通过、4 跳过、13 失败**。13 个失败来自既有配置默认值、模型环境、npm、Windows `msvcrt`、旧 state 字段以及环境中的 `python` 命令断言，未计入本功能通过结论。
- 已执行 Python 编译检查和 `git diff --check`；本次没有启动真实 Docker 容器，也没有重新执行手工 CLI/TUI 流程，因此 Docker 实际运行和人工交互仍未在本次验证中确认。
