# 功能：`/report` 用户级持久化与分层报告

## 本部分实现的功能（一览）

> **本部分 = "把当前 thread 的手动报告整理成分层内容，并保存为用户级、可追溯的报告快照"。**

**实现：**

1. **`/report` 手动生成报告** —— 使用现有报告生成逻辑生成当前 thread 的报告；
2. **用户级存储** —— 报告保存到 `~/.poirot/reports`，不写入项目目录；
3. **按日期归档** —— 使用报告生成时的本地时间分层到 `年/月/日`，文件名包含时间和标题；
4. **标题处理** —— `/report` 可指定标题，不指定时使用当前 thread 标题；
5. **报告元信息** —— 报告记录 thread、project、cwd、生成时间和原 thread session 位置；
6. **分层报告** —— 按概览、结构化摘要、`rag` 知识块组织内容，支持后续渐进式披露；
7. **对话副本** —— 额外保存只含 user/assistant 文本的 `conversation.jsonl`；报告中的工具活动只保留符号；
8. **可靠写入** —— 创建目录、处理非法文件名、避免覆盖同名报告，并使用临时文件和原子替换；
9. **可恢复读取** —— 报告和对话副本生成后可在新进程中读取，作为后续 FTS5/RAG 的输入。

**保持不变：**

- export 模式自动生成 `final_report` 和 `final_report.md` 的现有逻辑；
- 现有 reporter、ReportMiddleware、artifact store 和 run 输出目录；
- thread、checkpoint、项目和 sandbox 的现有存储布局；
- `/report` 生成失败时当前 thread 和原有 run 结果仍可继续使用。

**本部分不做：**

- ❌ SQLite FTS5、`/search` 和 Agent 原生检索；
- ❌ Embedding、向量数据库和本地文件知识源；
- ❌ 自动报告改存到 `~/.poirot/reports`；
- ❌ 报告列表、删除、重命名或版本合并命令。

## 在总计划中的位置

这是 **Thread 报告与分层 RAG** 的第一阶段，完成“报告内容整理 + 报告快照落盘”。后续阶段沿用本阶段生成的 Markdown、对话副本和来源元信息：

```text
P0  /report + 分层报告 + conversation.jsonl
    ↓
P1  SQLite FTS5 报告索引
    ↓
P2  /search + 摘要逐层展开
    ↓
P3  Agent 原生 search_reports 工具
```

本阶段必须产出可解析的 L0/L1/L2 内容和 L3 对话副本，但不实现索引、搜索或 Agent 自动调用。报告应当是独立快照；后续重新执行 `/report` 生成新快照，不修改旧文件。

## 存储布局

默认根目录沿用项目已有的 `storage_root`，没有覆盖时为当前用户的 `Path.home() / ".poirot"`：

```text
~/.poirot/
└── reports/
    └── {year}/{month}/{day}/
        ├── report-{HH-MM-SS}-{title}.md
        └── report-{HH-MM-SS}-{title}.conversation.jsonl
```

报告目录属于用户级存储，不属于当前项目目录，也不随进程的 `Path.cwd()` 改变。测试和部署可通过已有的 `storage_root` 配置写入临时目录。

日期和时间使用报告**生成时的本地时间**。文件名中的标题是安全化后的展示标题；不能让标题中的 `/`、路径穿越、控制字符或过长内容改变目录结构。生成时间相同且标题相同的报告不能互相覆盖，应追加稳定的 thread ID、短随机后缀或序号。

报告正文可以复用现有手动报告生成结果，但保存前必须整理成可逐层读取的 Markdown。为后续检索预留 frontmatter 或等价元信息，至少包含：

```text
name
description
thread_id
project
cwd
created_at
thread_location
conversation_location
report_created_at
schema_version
```

如果现有报告生成器还不能稳定提供这些字段，本阶段可以在保存层补齐。`thread_location` 必须指向原 session 目录；报告是快照，后续 thread 对话不会修改已经生成的文件。

报告至少包含以下层级：

- **L0 概览**：thread 要解决的问题、主要结果、状态和主题；
- **L1 结构化摘要**：关键决策、产出、限制、待办，以及每轮“用户想做什么 / Agent 完成什么”；
- **L2 `rag` 知识块**：可以独立理解、适合长期检索的事实、设计约定和结论；普通代码块和工具日志不自动进入其中。

同时生成 `conversation.jsonl` 作为 L3 深度来源，只保存 user/assistant 文本消息、轮次和顺序。tool/system 消息、工具参数和执行结果不进入副本；工具活动需要在报告中体现时只写 `[tool]` 等符号。L3 是报告生成时的筛选快照，完整 checkpoint 仍是 thread 历史的权威来源。

后续 RAG 默认只索引报告的标题、描述、概览、结构化摘要和明确标记的 `rag` 知识块；完整 checkpoint 不直接进入索引。报告中的来源字段必须足以让后续检索按项目和真实 `cwd` 过滤：默认搜索当前项目/cwd，`--all-projects` 由用户显式扩大范围。P0 只记录这些来源，不实现过滤或搜索。

## 与自动报告的边界

export 模式中的自动报告继续按现有流程运行：expert 模式使用 `final_state["final_report"]`，必要时调用 reporter fallback，再由 artifact store 按当前 `run_context.output_dir` 保存 `final_report.md`。default 模式仍按现有规则返回最后一条 AI 消息，不因本功能自动创建用户级报告。

`/report` 是单独的用户动作：它可以复用 reporter、middleware 产出的报告能力，但最终将内容保存到用户级 `reports/YYYY/MM/DD/`。不能把 `output_dir`、`logs_root`、thread session 目录或项目目录误当作手动报告目录，也不能通过修改 export 分支来实现。

## 验收行为

- `/report` 在当前 thread 有可生成内容时创建一个 Markdown 文件，并返回可读的绝对路径或相对用户存储根的路径。
- 同时创建匹配的 `conversation.jsonl`；Markdown 可明确区分 L0/L1/L2，副本可作为 L3 逐轮读取。
- `/report 自定义标题` 使用该标题；`/report` 使用当前 thread 标题。标题参数支持普通含空格文本，沿用现有命令解析习惯。
- 当前 thread 没有可用历史、报告生成器失败或保存失败时，给出明确错误；不创建空的“成功报告”，不破坏当前 runtime。
- 重启进程后，仍可读取该报告，且文件内容、来源 thread ID 和生成时间不变。
- 切换项目或 thread 后生成的报告仍按生成时的 thread/project/cwd 记录；报告路径按生成日期归档，不因后续重命名或切换而移动。
- 标题含中文、空格、换行、斜杠、引号、控制字符和超长文本时，文件名安全、可读且不越界。
- 同一个 thread 可以生成多份报告；旧报告不被覆盖，后续阶段可以按报告文件独立建立索引。
- tool/system 消息及工具执行结果不会进入对话副本；报告正文中的工具活动仅用简短符号或结果摘要表达。

---

## 提示词

````text
# 开发任务：`/report` 用户级持久化与分层报告

你是这个 Python Agent 项目的开发 Agent。请先阅读实际代码，再直接实施并验证本任务，不要只提交方案。项目已经完成 thread 持久化、项目目录绑定和 sandbox；本任务完成 `/report` 的用户级持久化、分层报告和筛选后的对话副本，不提前实现检索。

下文规定功能和验收行为，不限定新增文件、类名或函数签名。参考文档中的路径和模块名只是线索，必须以源码确认。优先复用项目已有的 report generator、ReportMiddleware、artifact store、storage_root 配置、原子写入和命令分发机制。

## 1. 目标与范围

实现 `/report [标题]`：针对当前 thread 生成一份分层手动报告及 user/assistant 对话副本，保存到用户级 reports 目录。两者必须是对应的独立快照，可在进程退出后读取，供后续 FTS5 和 RAG 功能使用。

本次必须交付：

- `/report` 命令的参数解析和当前 thread 上下文获取；
- 未指定标题时使用当前 thread 标题，指定标题时只影响本次报告；
- 按报告生成时的本地时间写入 `storage_root/reports/YYYY/MM/DD/`；
- 安全、稳定且不会覆盖旧文件的报告文件名；
- 报告来源元信息：thread、project、cwd、时间和原 session 位置；
- L0 概览、L1 结构化和逐轮摘要、L2 `rag` 知识块；
- 只含 user/assistant 对话正文的 `conversation.jsonl`，作为 L3 来源；
- 原子写入、失败处理、跨进程读取和自动化测试；
- 保持 export 模式自动报告行为完全兼容。

明确不在本次范围：

- 不解析或建立 `rag` 围栏索引；
- 不实现 SQLite FTS5、`/search` 或 `search_reports` Agent 工具；
- 不实现 Embedding、向量数据库、本地文件知识源、报告列表/删除/重命名；
- 不把自动 export 报告迁移到用户级 reports 目录；
- 不修改 thread、checkpoint、项目索引、sandbox 和项目目录布局。

如果现有 `/report` 已经生成内容，优先复用该生成能力，再把结果整理为分层报告；不要为了本任务重写无关报告算法。若现有 `/report` 尚未存在，使用项目已有 reporter 能力建立手动入口，但保持 export 分支独立。

## 2. 开始前的源码核查

先查明并记录简短实施计划，然后继续开发：

1. `/report` 是否已经存在，命令如何进入主循环，以及同步/异步生命周期；
2. 自动 export 报告在哪个入口生成，`final_report`、artifact 和 `output_dir` 的边界是什么；
3. reporter、ReportMiddleware、artifact store 是否可以复用，报告生成是否依赖完整 final state 或 run_context；
4. `storage_root` 从哪里配置，thread session 目录如何定位，`ThreadMeta` / `AppRuntime` 如何获取当前 thread、project、cwd；
5. 当前项目是否已有 JSON/Markdown 原子写入、slug 清理、时区和日志约定；
6. CLI/TUI 是否共享命令分发，运行中的对话、错误和退出路径如何处理。
7. checkpoint 中 user、assistant、tool、system 消息的实际类型与字段，如何只提取可供报告使用的对话文本；
8. 现有 reporter 是否能提供逐轮摘要，若不能，如何最小范围补齐而不改变 export 自动报告。

不要读取真实用户 `~/.poirot` 来验证，也不要用项目工作目录作为持久化默认路径。不要因为文档中的假设直接新增第二套配置或报告生成路径。

## 3. 报告目录和文件名

- 默认报告根目录为已有 `storage_root / "reports"`；若没有覆盖配置，storage root 展开为 `Path.home() / ".poirot"`。
- 报告路径为 `reports/YYYY/MM/DD/report-HH-MM-SS-{safe_title}.md`。年/月/日和时分秒使用报告生成时的本地时间，并保持现有项目的时区和 ISO 时间字段约定。
- 同一报告旁保存 `report-HH-MM-SS-{safe_title}.conversation.jsonl`；发生同名冲突时，两份文件必须使用相同的最终后缀，避免错配。
- 目录创建是幂等的，不能因为当前项目目录不可写就回退到项目目录或内存后声称成功。
- 标题安全化必须阻止 `/`、`\\`、`.`/`..` 造成路径穿越，移除或替换控制字符，压缩不必要空白，并限制文件名长度。保留正常中文、英文、数字、空格和常用标点。
- 空白标题报错，不能生成名为 `report--.md` 的报告。标题清理后为空时使用明确的安全后备名或报错，行为要稳定。
- 同一时间和标题不能覆盖旧报告。使用追加 thread ID、序号或其他稳定后缀，并在返回信息中报告最终路径。
- 两份文件都先写同目录临时文件，完成并 flush/fsync 后原子替换。设计提交次序和失败清理，不能报告“成功”却只留下 Markdown 或只留下副本；临时文件不能被后续索引误识别为正式报告。

## 4. 报告内容和元信息

报告正文优先使用现有手动报告结果。保存层可以在正文前加入 frontmatter；如果现有格式不适合 frontmatter，使用项目已有的等价元信息格式，但必须能稳定解析以下信息：

- `name`：本次报告标题；
- `description`：简短的报告说明；
- `thread_id`：完整 thread ID；
- `project`：生成时的项目名，没有绑定时按现有元数据语义处理；
- `cwd`：生成时 thread 元数据中的规范化路径；
- `report_created_at`：带时区的报告生成时间；
- `thread_location`：原 session 目录位置；
- `conversation_location`：本次报告的对话副本位置；
- `schema_version`：报告格式版本，便于后续 RAG 解析。

不要把当前进程的 `Path.cwd()` 当作已绑定 thread 的 cwd。没有 cwd 的旧 thread 不要被本次报告隐式绑定；按现有兼容策略记录空值或明确标记未知。

报告路径和 frontmatter 中的来源信息不能指向临时文件。写入完成后返回的 artifact/result 应包含最终报告路径、报告标题和 thread ID，供 CLI 提示和后续索引使用。

### 分层内容

最终 Markdown 至少有三个可识别层级：

- L0 概览：目标、主要结果、状态、主题；
- L1 结构化摘要：关键决策、产出、限制、待办；每轮只概括用户目标和助手结果；
- L2 详细知识块：用 `rag` 围栏标记可独立理解的事实、设计约定和结论。普通代码块、日志不自动转为 `rag` 块。

摘要不需要复述长 prompt、工具调用参数和推理过程。现有 reporter 的内容可以复用，但必须能被整理为上述层级。没有足够信息时不要编造决策或结果；标明未知或省略该项。

### L3 对话副本

- 从当前 thread 的已保存历史中提取 user/assistant 文本消息，按原顺序和轮次写入 JSONL。
- 每行至少包含 `turn`、`role`、`content`；可以增加必要的时间和来源字段。
- 不保存 system、tool 消息，也不保存工具调用参数和执行结果。工具活动若需要在报告中体现，只写 `[tool]`、`[tool: shell]` 等符号；不为工具调用创建对话副本记录。
- 非文本或无法安全序列化的消息跳过或明确标记；不把完整 checkpoint 直接序列化成对话副本。
- 原 checkpoint 保持完整且不被修改；报告和副本是同一生成时刻的快照。

## 5. `/report` 命令行为

至少支持：

`/report`、`/report 自定义报告标题`

- `/report` 使用当前 thread 的标题；若当前 thread 没有可用标题，沿用项目已有的安全默认标题规则，不调用额外 LLM 只为补标题。
- 标题可包含空格；按现有命令解析规则把命令后的剩余文本作为标题，不要求普通标题额外加引号。
- 命令必须在当前对话不处于不可安全重入的运行状态时执行；如果项目已有“运行中拒绝命令”的规则，复用它。
- 成功时显示报告已生成及保存位置；失败时显示可诊断原因，不吞掉异常，也不把失败伪装成普通聊天回复。
- `/report` 不切换 thread，不创建新 thread，不更新 thread 标题，不改变 project/cwd，不改变 sandbox，不把报告内容追加回当前对话。
- 再次执行 `/report` 生成新的快照，不覆盖旧报告；报告是否更新 thread 的 `updated_at` 按现有 metadata 语义决定，但不要把“生成报告”误记成一轮用户对话。

## 6. 自动 export 报告兼容性

找到现有 export 分支并为其增加回归测试。保持以下行为：

- expert 模式优先使用 middleware 已写入的 `final_report`，没有时才使用 reporter fallback；
- artifact 仍保存到当前 `run_context.output_dir`，文件名和 metadata 仍按原逻辑；
- default 模式仍按原规则返回最后一条 AI 消息，不因 `/report` 持久化改为自动保存用户级报告；
- `report.generated` journal/event 的原有语义不被破坏；
- 自动报告与手动 `/report` 可以内容相似，但路径、触发条件和生命周期是两条明确的流程。

不要通过修改 export 逻辑、改变 `output_dir` 或复制 artifact 来“顺便”完成手动报告保存。

## 7. 测试与验收

所有测试使用临时 storage root 和临时 thread/session 数据，不能污染开发者真实 `~/.poirot`。先运行新增的报告、分层内容和对话副本测试，再运行受影响的 runtime、CLI、reporter、artifact 和 thread 测试，最后按项目要求运行全量测试。

### 存储测试

- 默认配置生成在用户级 storage root 的 `reports` 下，不在项目 cwd、output root、logs root 或 thread session 目录下；覆盖配置时路径跟随临时 root。
- 报告自动创建年/月/日目录，日期和时间取报告生成时刻，frontmatter 的时区字段可解析。
- 标题缺省使用 thread 标题；自定义标题支持中文、空格和常用标点。
- 标题包含换行、斜杠、反斜杠、`..`、控制字符和超长内容时不越界、不破坏文件名，最终路径稳定可预测。
- 同秒同标题生成两次不覆盖第一份；每个 Markdown 均能定位到自己的对话副本。
- 任一文件写入失败、目录不可创建或原子替换失败时返回失败，不留下被误认作完整报告的单边文件；临时文件清理，旧报告不被截断。
- 新进程可以按保存路径读取完整 Markdown、来源元信息和匹配的对话副本。

### 分层内容与对话副本测试

- L0 能说明目标、结果和状态；L1 包含关键决策、产出、限制及逐轮的用户目标和助手结果；L2 `rag` 块有可独立理解的来源内容。
- 普通 `python` 代码块和日志不会被误标成 `rag` 知识块。
- 多轮对话按原顺序保存，user/assistant 文本可读取；system、tool、工具参数和执行结果不进入副本。
- 工具活动需要体现时只出现简短符号；非文本消息跳过或被明确标记。
- thread 继续对话后，已有报告与副本内容不变；再次 `/report` 产生新快照。
- 空历史、摘要材料不足或生成失败时的行为明确，不编造不存在的决策或结果。

### 命令与 runtime 测试

- `/report`、`/report title`、空标题和未知参数的行为明确；含空格标题不需要额外引号。
- 生成报告后当前 thread、project、cwd、sandbox 和 checkpoint 不改变；切换 thread 后报告来源属于生成时的目标 thread。
- 没有历史或 reporter 失败时不创建伪成功文件；当前 runtime 仍可继续使用。
- 报告生成不把 tool/system 消息变成后续检索可用正文；对话副本只包含受允许的内容。

### 自动报告回归

- expert/default 两种 export 模式保持既有返回值、artifact 路径和 journal 行为；
- 自动 export 不会意外在 `~/.poirot/reports` 生成第二份文件；
- `/report` 与自动 export 连续执行时互不覆盖、互不改变对方的 output root。

### 实际流程

- 通过真实应用装配入口启动，执行一次对话后运行 `/report`，检查返回路径和文件内容；
- 退出并重新启动，用同一临时 storage root 读取报告；
- 至少验证一次 TUI 或其他共享入口不会因为命令注册变更而启动失败；
- 若无法执行某个交互流程，说明具体原因，不用单元测试代替手工验收。

## 8. 完成标准

只有 `/report` 可以稳定生成分层 Markdown 和匹配的安全对话副本、跨进程读取、处理失败和非法标题，并且 export 自动报告回归通过，才能宣布本阶段完成。

最终报告应说明：

1. 实际复用的报告生成和 artifact 组件；
2. 报告路径、标题清理和冲突处理规则；
3. L0/L1/L2 报告格式、L3 对话副本、来源元信息及兼容旧 thread 的处理；
4. 执行过的测试命令、结果和真实 CLI/TUI 验证；
5. 尚未实现的 FTS5、`/search`、Agent 工具、Embedding 和本地文件扩展。

保留用户已有改动，不修改无关功能，不通过降低持久化或自动报告验收标准来使测试通过。
````
