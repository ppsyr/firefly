# dir based session 功能完成情况

## 1. thread 持久化与恢复

### 已完成功能

- **跨进程恢复会话**：LangGraph 的完整 thread checkpoint 已持久化到用户目录。退出并重新启动后，可选择原会话继续对话；不同会话的消息和状态相互隔离。
- **会话元数据与标题**：新会话先保留内存中的 thread ID；用户首次发送消息后才保存 ID、标题、创建时间和更新时间。首次用户消息自动生成标题，不调用 LLM；用户可手动重命名，后续对话不会覆盖手动标题。
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

- `ThreadStore` 按 `thread_id` 定位会话目录；交互中新 thread 首次发送前只保留 pending 元数据，发送时才写入 `metadata.json`，列出时按 `updated_at` 降序排序。元数据写入使用同目录临时文件、`fsync` 和原子替换；无效元数据会发出警告，不会被自动删除。
- `SessionCheckpointer` 按会话把同步与异步的 LangGraph checkpoint 调用路由到对应目录的 `checkpoints.db`。底层 `SQLiteCheckpointer` 使用 `AsyncSqliteSaver`；连接由应用运行时持有并在关闭时释放。
- 切换会话前先校验元数据并读取目标 checkpoint，读取失败时保持当前 runtime；切换成功后沿用目标会话目录。新会话在首次用户消息时物化元数据并更新标题；手动重命名由 `title_set` 保护。
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
- 新 thread 首次发送消息时才写入 `project` 和 `cwd`。`/thread new` 使用当前项目；旧的未绑定 thread 仍能通过全局 `/thread` 恢复，不会被自动绑定。
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

## 4. 同目录会话引用 `@thread-id`

### 已完成功能

- **引用语法**：支持 `@thread-<id>`、`@<thread-id>`、已保存的单词标题，以及带引号的多词标题 `@"会话标题"`。标题不会为本功能重新生成摘要或标题。
- **实时候选搜索**：CLI 和 TUI 在用户输入 `@`、thread ID 前缀或标题片段时同步展示同目录候选；标题搜索支持普通文本、中文和大小写不敏感的标题子串匹配。
- **交互选择**：候选列表支持上下键移动和回车确认；只有一个候选时也必须回车确认。确认后插入完整 thread ID 或带引号标题并保留尾部空格，用户可以继续输入本轮问题；TUI 会保留已选择 thread 的确认标识。Esc 或未确认不会由下拉流程自动注入会话历史。
- **同目录隔离**：只允许引用当前 thread 之外、规范化真实 `cwd` 相同的会话。不同项目名但指向同一真实目录可以引用；同名项目但目录不同、相似前缀目录、符号链接越界以及缺少 `cwd` 的旧 thread 均被拒绝。
- **历史引用**：从目标 thread 最新已提交 checkpoint 读取消息，不切换当前 runtime，也不修改目标 thread。只保留按原顺序排列的 User/Assistant 文本消息，过滤 system、tool、工具调用参数和结果以及非文本内容。
- **上下文预算**：默认最多保留最近 10 轮、4,000 token；超出时优先保留最近对话，并在引用材料中标注历史已截断。引用内容只进入当前轮 enriched prompt，不会在后续轮次自动重复注入，也不参与当前 thread 标题生成。
- **失败与歧义处理**：未找到、标题重名、短 ID 多命中、标题与文件名冲突、目标已删除、checkpoint 损坏或历史不可读时不会注入内容，并给出明确提示；交互选择取消时保持原问题不变。
- **新会话延迟建立**：`/thread new`、项目切换和启动时预留的 thread 只保留内存状态，不写入 session metadata 或恢复索引。用户首次发送聊天信息时才物化 metadata、session 目录和项目索引，空会话不会出现在恢复列表。

### 实现方式

- `agents/runtime/thread_quote.py` 负责引用解析、实时候选、真实目录比较、ID/标题消歧、checkpoint 读取、消息角色过滤和 10 轮/4,000 token 截断；读取历史直接使用当前 `SessionCheckpointer`，不调用 `switch_thread`。
- `app/bootstrap.py` 在 `prepare_question` / `aprepare_question` 中先解析同目录 thread 引用，再与现有 `@文件` 预处理合并。原始用户问题保留给标题和运行记录，引用材料只追加到本轮 enriched prompt。
- `app/cli/command_completer.py` 和 `app/cli/main.py` 使用 `prompt_toolkit` 的实时补全和回车接受逻辑；`app/tui/app.py` 使用 Textual 输入变化事件、后台候选刷新、上下键移动和回车确认。文件候选与 thread 候选共用 `@` 输入入口，并继续执行各自的边界检查。
- `runtime/threads.py` 为新交互 thread 增加 pending 状态。`ThreadStore.materialize()` 在第一次发送前将 metadata 原子写入正式 session 目录并同步项目索引；切换或退出未发送的 pending thread 会丢弃内存预留，不留下恢复条目。

### 验证

- 同目录 thread 引用、消息过滤、标题/短 ID 消歧、历史预算截断、无绑定目录和损坏历史测试已覆盖；CLI 补全测试覆盖 ID、单词标题、中文标题、带引号多词标题以及单候选回车确认。
- 受影响测试命令：

  ```bash
  .venv/bin/pytest -q \
    poirot/backend/tests/v1/unit/runtime/test_thread_quote.py \
    poirot/backend/tests/v1/unit/cli/test_command_completer.py \
    poirot/backend/tests/v1/unit/runtime/test_file_access.py \
    poirot/backend/tests/v1/integration/test_thread_persistence.py \
    poirot/backend/tests/v1/integration/test_project_binding.py
  ```

- 最近一次上述受影响测试结果为 **53 项通过**；Python `compileall` 和针对本次修改文件的 `git diff --check` 通过。全量测试结果为 **2757 通过、4 跳过、13 失败**；失败项来自既有配置默认值、模型环境、npm、Windows `msvcrt`、旧 state 字段及环境中的 `python` 命令断言，与本功能无关。
- 尚未在本次验证中重新执行真实 CLI/TUI 的人工交互流程，也未启动 Docker 容器；自动化测试覆盖了共用消息处理链路和选择器逻辑。


## 5. `/add-dir` 追加会话引用目录

### 已完成功能

- **会话级追加目录**：执行 `/add-dir <目录>` 后，当前 thread 的后续 `@文件` 引用可以从该目录读取；可添加多个目录，目录只对当前 thread 生效，不改变 `thread.cwd`、项目绑定、进程工作目录或 sandbox 权限。
- **目录管理命令**：`/add-dir` 无参数时列出当前 thread 已追加的目录；`/add-dir --remove <目录>` 移除目录；目录不存在、不可访问或未添加时给出明确提示。路径支持现有 CLI 的引号写法，空格路径可以正常处理。
- **规范化与去重**：添加目录时校验目录存在且可访问，并保存真实绝对路径。重复添加同一目录，以及通过符号链接添加同一真实目录，不会产生重复项；当前 `cwd` 也不会被重复加入。
- **查找优先级**：`@文件名` 先查当前 thread 的 `cwd`；当前目录没有命中时，再按追加顺序查找 `extra_dirs`。同一优先级有多个命中时继续使用原有候选选择流程。带相对路径的引用沿用相同优先级，绝对路径只能落在 `cwd` 或已追加目录内。
- **安全边界**：查找、读取和候选补全均按真实路径判断目录归属，拒绝 `../` 穿越、越界绝对路径和指向允许范围外的符号链接。原有单文件大小、二进制、UTF-8、目录清单和上下文预算限制保持不变。
- **失效目录处理**：追加目录不会预先读取或注入文件；目录之后被删除、移动或失去访问权限时，不会扩大搜索范围，引用只返回文件不存在或不可访问提示。
- **CLI/TUI 共享状态**：CLI 和 TUI 都从当前 thread 元数据构造 `ThreadFileAccess`；CLI 补全支持 `--remove`，并列出当前 thread 的追加目录，带空格的目录会自动使用引号。

### 存储位置

- 追加目录保存在当前 thread 的 session 元数据中：

  ```text
  {storage_root}/sessions/YYYY/MM/DD/thread-{HH-MM-SS}-{thread_id}/metadata.json
  ```

- 元数据字段为 `extra_dirs`，内容是按添加顺序排列的真实绝对路径列表。旧 thread 没有该字段时按空列表读取；不新增独立目录、checkpoint 或项目索引布局。
- pending thread 在首次发送时物化时一并保存追加目录；thread 切换、项目切换和跨进程恢复时只读取目标 thread 自己的列表，移除后不会在恢复时重新出现。

### 实现方式

- `runtime/threads.py` 为 `ThreadMetadata` 增加 `extra_dirs`，通过 `ThreadStore.add_extra_dir()` 和 `remove_extra_dir()` 使用现有原子元数据写入流程持久化目录列表。
- `app/bootstrap.py` 为 `AppRuntime` 提供追加和移除目录的方法，并在同步、异步 `prepare_question` 中把当前 thread 的 `extra_dirs` 传给 `ThreadFileAccess`。
- `agents/runtime/file_access.py` 负责 cwd 与追加目录的优先级搜索、相对路径候选、绝对路径边界检查、真实路径校验和文件读取；追加目录只扩展 `@文件` 只读引用范围，不扩展 sandbox 文件工具权限。
- `app/cli/commands.py` 注册 `/add-dir`；`app/cli/main.py` 和 `app/tui/app.py` 的候选缓存都按 thread ID、cwd 和追加目录列表区分，避免切换 thread 后复用旧范围。
- `app/cli/command_completer.py` 提供 `/add-dir --remove` 及已添加目录的补全；TUI 沿用现有文件候选展示和确认流程。

### 验证

- 针对性测试覆盖追加后引用、移除后拒绝、cwd 优先、多个追加目录按顺序产生候选、相对路径、绝对路径、重复添加、符号链接去重、空格路径、失效目录、thread 隔离和元数据恢复。
- 受影响测试命令：

  ```bash
  .venv/bin/pytest -q \
    poirot/backend/tests/v1/unit/runtime/test_file_access.py \
    poirot/backend/tests/v1/unit/cli \
    poirot/backend/tests/v1/unit/tui \
    poirot/backend/tests/v1/unit/sandbox/test_local_runtime.py \
    poirot/backend/tests/v1/integration/test_thread_persistence.py \
    poirot/backend/tests/v1/integration/test_project_binding.py
  ```

- 上述受影响测试共 **119 项通过**；Python `compileall` 和 `git diff --check` 均通过。
- 全scm-history-item:/Users/lucia/Desktop/Projects/code-python/agent-practice/firefly?%7B%22repositoryId%22%3A%22scm0%22%2C%22historyItemId%22%3A%225da9ce851a7d80be341a0ea66eda7928e31cc1e2%22%2C%22historyItemParentId%22%3A%22e40579ddaf3d0b69fec11190d244bac0c379cbf8%22%2C%22historyItemDisplayId%22%3A%225da9ce8%22%7D量测试结果为 **2761 通过、4 跳过、14 失败**。失败项来自既有配置默认值、模板版本、npm/PATH、平台锁和 `python` 命令环境问题，与本次 `/add-dir` 功能无关。
- 已通过自动化命令处理、补全、文件引用和持久化链路验证；尚未重新执行真实交互式 CLI/TUI 人工流程，也未启动 Docker 容器。


## 6. `/cd` 切换目录并创建新会话

### 已实现功能

- **切换目录并新建 thread**：`/cd <目录>` 创建新的 thread ID，绑定目标目录及其项目，再将当前会话切到新 thread。即使目标与当前 `cwd` 是同一真实目录，也会新建 thread；原 thread 的 ID、`cwd`、历史、checkpoint 和追加目录不变。新 thread 不继承历史或 `/add-dir` 的 `extra_dirs`。
- **路径与项目规则**：支持 `~`、相对路径和符号链接，统一解析为真实绝对目录；相对路径以当前 thread 的 `cwd` 为基准。无 `cwd` 的旧 thread 只能使用绝对路径。目标不存在或不是目录时拒绝；同一真实目录复用已注册项目，新目录按 `--dir` 的默认规则以目录名注册项目。项目名已绑定其他目录时明确报错，不覆盖原绑定，也不调用 `os.chdir()`。
- **会话与权限恢复**：`/thread switch <旧 ID>` 根据目标 thread 元数据恢复项目和逻辑 `cwd`；文件引用使用目标 thread 的 `cwd`、`extra_dirs`，sandbox 上下文在切换时清空，并释放上一会话的 sandbox 资源。运行中的对话会阻止切换。
- **CLI/TUI 入口**：两者共用 `/cd` 命令处理，支持用引号传入含空格的目录；切换成功后更新当前 thread 和界面状态，失败时显示错误。CLI 提供目录候选补全，TUI 命令面板列出 `/cd`。

### 持久化与实现

- 新 thread 创建时先在 `ThreadStore` 中保留 pending 元数据，包含新 ID、项目及 `cwd`，但**尚不写入磁盘**。首次发起对话时才物化到其 session 目录并进入项目 thread 索引；未发起对话就切走的 pending thread 会被删除。这沿用现有新会话生命周期，因此“立即创建”指当前进程中立即可用，不表示立即持久化。
- 已物化的旧、新 thread 各自继续使用原有 session 目录保存元数据、checkpoint、运行日志和产物；不搬迁或复制数据。`AppRuntime.cd()` 先解析目录并确认项目绑定，再创建 pending thread，通过现有 `switch_thread()` 切换。目录、项目或切换准备阶段失败时保留原 runtime，并清理这次创建的 pending thread。

### 验证与待确认项

- 新增测试覆盖 A 到 B 的新 ID、目录和项目绑定、旧 thread 保留、`extra_dirs` 不继承、B 首次对话前不进入项目索引、同目录再次 `/cd`、相对路径、目录不存在、项目名冲突、无 `cwd` 旧 thread 的相对路径限制，以及 checkpoint 读取失败时的 pending thread 清理。命令和补全单测也已覆盖。
- 本次验证命令：

  ```bash
  .venv/bin/pytest -q poirot/backend/tests/v1/integration/test_cd_command.py poirot/backend/tests/v1/unit/cli/test_thread_commands.py poirot/backend/tests/v1/unit/cli/test_command_completer.py
  .venv/bin/pytest -q poirot/backend/tests/v1/integration/test_cd_command.py poirot/backend/tests/v1/integration/test_project_binding.py poirot/backend/tests/v1/integration/test_thread_persistence.py poirot/backend/tests/v1/unit/cli poirot/backend/tests/v1/unit/tui poirot/backend/tests/v1/unit/sandbox/test_local_runtime.py
  ```

- 上述测试分别为 **22 项通过**、**113 项通过**。前一轮全量测试为 **2768 项通过、4 项跳过、13 项失败**；失败涉及既有配置断言、环境依赖、平台差异和缺少系统 `python` 命令，未见 `/cd` 测试失败。本次文档更新没有重新运行全量测试。
- 尚未对 `/cd` 单独完成真实 CLI 的 A → B → 旧 thread 操作、TUI 交互、跨进程切回、sandbox 与 `@文件` 的 A/B 权限边界、`~`/符号链接/普通文件路径，以及切换后段异常的回退测试。目录补全目前只做了基本覆盖；这些场景不能算作已验证完成。
