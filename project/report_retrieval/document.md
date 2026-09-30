# report retrieval 功能完成情况

## 1. `/report` 持久化与分层报告

### 已完成功能

- 支持 `/report` 和 `/report 自定义标题`，针对当前 thread 创建手动报告快照。
- 未指定标题时使用当前 thread 标题；自定义标题只影响本次报告，不修改 thread 标题。
- 报告使用现有 `storage_root`，默认位于用户目录的 `~/.poirot/reports`，不写入项目目录、进程当前目录或 run 的 `output_dir`。
- 报告按生成时的本地时间分层保存：

  ```text
  {storage_root}/reports/
  └── YYYY/MM/DD/
      └── report-HH-MM-SS-title/
          ├── report-HH-MM-SS-title.md
          └── report-HH-MM-SS-title.conversation.jsonl
  ```

- 同一秒生成同标题报告时追加序号，不覆盖旧报告；两个文件始终使用相同的报告目录和文件名前缀。
- 标题会进行 Unicode 规范化、空白压缩、控制字符处理、路径分隔符处理和长度限制，阻止路径穿越。
- 报告 Markdown 使用 frontmatter，包含：
  - `name`
  - `description`
  - `thread_id`
  - `project`
  - `cwd`
  - `report_created_at`
  - `thread_location`
  - `conversation_location`
  - `schema_version`
  - `location`
- `cwd`、`project` 和 `thread_location` 来自生成时的 thread 元数据，不使用进程当前目录；无绑定目录的旧 thread 保持空值语义，不会被隐式绑定。
- 报告正文包含可识别的三层内容：
  - **L0 Overview**：目标、结果、状态、主题和待办数量。
  - **L1 Structured Summary**：关键决策、交付结果、来源、限制、待办，以及每轮 user/assistant 目标和结果摘要。
  - **L2 Knowledge**：使用 `rag` 围栏保存可独立检索的事实和结论；普通代码围栏和工具日志不会自动转为知识块。
- 报告旁的 `conversation.jsonl` 只保存当前 checkpoint 中的 user/assistant 文本消息，按原顺序写入 `turn`、`role` 和 `content`；system、tool 消息、工具参数和工具结果不会写入。
- 报告和对话副本都是生成时快照。后续 thread 对话不会修改旧文件，再次执行 `/report` 会创建新快照。
- 写入使用同目录临时文件、flush、`fsync` 和原子替换；任一阶段失败时清理临时文件和已写入的另一份文件，不留下可被误认为完整报告的单边快照。
- `AppRuntime.generate_report()`、CLI 和 TUI 共用同一持久化入口；生成报告不会切换 thread、创建 thread、修改 project/cwd、改变 sandbox 或写入 checkpoint。
- 自动 export 报告逻辑保持原有边界：expert 模式仍使用当前 run 的 `output_dir/final_report.md`，default 模式仍不自动保存用户级报告。

### 实现位置

- `poirot/backend/agents/reporting/report_store.py`：报告目录、标题清理、分层 Markdown、对话副本和原子写入。
- `poirot/backend/agents/reporting/thread_report.py`：从当前 thread checkpoint 获取状态并调用现有 reporter。
- `poirot/backend/app/bootstrap.py`：提供 `AppRuntime.generate_report()`。
- `poirot/backend/app/cli/main.py`：CLI `/report` 展示报告和最终路径。
- `poirot/backend/app/tui/app.py`：TUI `/report` 展示生成结果。

### 验证

- 报告模块和 conversational mode 测试：**23 项通过**。
- 覆盖用户级路径、日期目录、独立报告目录、同名冲突、非法标题、完整 frontmatter、L0/L1/L2、user/assistant-only JSONL、快照文件配对和写入失败清理。
- `compileall` 和 `git diff --check` 通过。
- 已通过跨进程读取保存后的 Markdown 和 JSONL 文件验证。
- 尚未重新执行真实模型驱动的交互式 CLI/TUI `/report` 手工流程；CLI/TUI 自动化测试已通过。

## 2. SQLite FTS5 报告索引

### 已完成功能

- 在 `{storage_root}/rag/index.sqlite` 建立可重建的 SQLite FTS5 派生索引；不会把索引内容写回报告、conversation JSONL、checkpoint 或项目文件。
- 只扫描 `reports/YYYY/MM/DD/report-.../report-....md` 正式报告，解析 schema version 1 的 frontmatter、L0、L1 和明确的 `rag` 围栏；普通代码围栏、工具日志、L3 对话副本和自动 export 不进入索引。
- 每个报告拆成带来源字段的检索块，保存 `report_id`、相对 `report_path`、`thread_id`、`project`、规范化 `cwd`、`section`、`level`、轮次、内容 hash 和报告生成时间。
- `/report` 两份快照成功后自动增量索引；同一报告重复建立索引不会产生重复块，正文变化会在一个事务中替换旧块。索引失败不撤销已保存报告，并通过 `ReportArtifact.index_error` 和 CLI/TUI 提示诊断信息。
- `/report` 不会全量扫描 `reports` 目录：每次只解析并写入本次新生成的单份报告。全量扫描仅由显式的 `ReportIndex.rebuild()` 触发，因此日常报告生成的索引开销与当前报告大小相关。
- `ReportIndex.rebuild()` 使用新数据库重建并原子替换旧索引，跳过损坏、缺字段、未知版本、不可读和报告目录外符号链接，同时清除已删除或移动报告的陈旧条目。
- `ReportIndex.search(query, project=..., cwd=..., limit=...)` 提供有界内部查询接口，返回报告路径、章节、层级、轮次、内容和来源字段；查询前独立应用 project/cwd 过滤，并校验报告仍可读取。
- 中文查询使用与写入一致的二元词辅助字段；英文、混合文本、文件名/配置键及 thread 标识符保留原文和精确匹配能力。FTS 特殊字符、空查询和无命中返回稳定结果。

### 实现位置

- `poirot/backend/agents/reporting/report_index.py`：报告解析、FTS5 schema、中文二元词、增量索引、重建和查询。
- `poirot/backend/agents/reporting/thread_report.py`：手动报告成功保存后触发增量索引。
- `poirot/backend/app/cli/main.py`、`poirot/backend/app/tui/app.py`：显示索引失败但保留报告成功状态。
- `poirot/backend/tests/v1/unit/reporting/test_report_index.py`：索引、重建、过滤和中英文查询测试。

### 验证

- 索引与报告模块测试通过；覆盖分层内容、代码围栏排除、重复/修改替换、中文和英文查询、project/cwd 过滤、坏文件跳过、删除清理和特殊查询。
- 已验证独立进程使用同一 `storage_root` 写入并查询索引；删除或损坏索引后可通过重建恢复，不影响报告正文。
- 索引数据库是可丢弃派生数据；删除后可直接调用 `ReportIndex.rebuild()` 从 `reports` 目录恢复。

## 3. `/search` 报告检索与渐进式披露

### 已完成功能

- 采用 `/search <查询>` 作为历史报告检索命令，并支持 `/search --all-projects <查询>` 显式扩大到用户级全部项目。
- 默认范围同时限制当前 thread 的 `project` 和规范化真实 `cwd`；无绑定范围时拒绝默认搜索，不退回进程当前目录。`--all-projects` 只扩大报告检索，不改变 thread、项目、sandbox 或文件工具权限。
- 复用模块二 FTS5 索引，支持中文、英文、混合查询和普通 FTS 特殊字符；查询参数按普通文本解析，不直接作为可信 FTS 表达式执行。
- 结果按报告路径聚合并去重，保留报告相对路径、命中章节和层级。每个候选先返回 L0/L1 摘要；命中依据在 L2 时展开有限知识块；必要的深层核对只读取配对 JSONL 中最多 3 条 user/assistant 文本轮次，不读取 checkpoint、tool 或 system 内容。
- 输出设置有界：最多向回答模型提供 5 份报告；每个报告块最多约 1800 字符，总检索上下文最多约 9000 字符。报告路径和对话副本再次校验在用户级 `reports` 根目录内且仍可读。
- 检索结果按报告级词项覆盖率排序，优先保留覆盖率更高的报告；不把 SQLite FTS5 的 `bm25` 原始值直接当作置信度。当前最低相关度阈值为 `0`，即 FTS5 召回后暂不再按词项覆盖率拒绝候选。先前使用 `0.5` 时，类似“我之前 report 过哪些 test”的自然语言问题会误拒绝实际相关的报告，因此已放宽该门槛。
- `/search` 不再把命中的报告正文直接返回给用户。命中报告的 L0/L1 摘要、必要的 L2 知识块和有限 L3 对话片段会与用户原问题一起交给 `researcher` 模型，由模型生成最终回答，并附带报告路径来源。
- 模型调用失败时返回明确的失败提示和有界检索证据作为回退；搜索过程不会把用户问题、检索资料或模型回答写入当前 thread checkpoint。
- 空查询/参数错误、索引缺失和无命中分别给出提示；索引读取时跳过已失效的报告。搜索是只读操作，不隐式重建索引、不修改当前 thread 或持久化消息。

### 实现位置

- `poirot/backend/agents/reporting/report_search.py`：命令参数解析、范围校验、FTS 候选聚合、相关度排序与可配置门槛、L0/L1/L2/L3 有界展开，以及将检索证据交给模型生成回答。
- `poirot/backend/agents/reporting/report_index.py`：补充按报告读取已索引块的内部接口。
- `poirot/backend/app/cli/commands.py`：注册 `/search`，CLI/TUI 共用同一处理入口。
- `poirot/backend/tests/v1/unit/reporting/test_report_search.py`：命令、范围、分层展开和错误反馈测试。

### 验证

- 当前 `/search` 的 4 个单元测试通过，覆盖命令解析、默认 project/cwd 过滤、显式跨项目、L2 命中、无绑定范围和索引缺失反馈。
- 回答服务尚缺自动化测试：需要补测最多 5 份报告、原问题与证据共同传入模型、来源输出、模型失败回退及不写入 thread 状态。尚未验证真实模型驱动的 CLI/TUI 交互流程。
- 相关度门槛目前为 `0`，因此不能声称已经实现“只有达到最低契合度才命中”。后续应使用针对中文自然语言问题校准过的策略恢复有效门槛，并补上“列举历史报告”类查询的回归测试。
- 本阶段没有引入 Embedding、向量数据库或 Agent 自动检索；只有用户显式执行 `/search` 时才进行一次 FTS 检索和一次 researcher 模型回答。

## 4. Agent 原生报告检索

未实现独立的 Agent 工具。本阶段的 `/search` 是显式命令服务，不会由 Agent 在普通对话中自动触发；检索报告数和上下文字符预算由回答服务固定限制。

## 5. 附加功能 1：普通本地文件知识源

未实现。本阶段不导入普通本地文件或目录，也不生成文件知识源快照。

## 6. 附加功能 2：Embedding 与混合检索

未实现。本阶段不依赖 Embedding 服务、向量数据库或混合排序。

Embedding 阶段的预期边界是：报告入库时按检索块批量生成并持久化向量，查询时只为用户问题生成查询向量，再将少量召回块交给后续回答模块；批量处理可以降低请求次数，但总计算量仍随报告文本量增长。本阶段仅提供可复建的 FTS5 关键词索引，不提前引入该成本。后续实现应继续复用本阶段的报告格式、来源元信息和 L0/L1/L2/L3 分层协议。
