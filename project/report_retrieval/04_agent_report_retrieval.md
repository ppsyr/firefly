# 功能：Agent 原生报告检索

## 本部分实现的功能（一览）

> **本部分 = "把模块三的报告检索能力作为 Agent 可选择调用的工具，让 Agent 在确实需要历史背景时主动查找报告"。**

**前置功能已完成：**

- 模块一已实现 `/report [标题]`：在用户级 `reports/YYYY/MM/DD/` 保存分层 Markdown（L0 概览、L1 摘要、L2 `rag` 知识块）和 user/assistant-only 的 L3 `conversation.jsonl`；原 checkpoint 保存完整 thread 状态，export 自动 `final_report.md` 逻辑不变。
- 模块二已实现 SQLite FTS5 报告索引：支持增量更新、全量重建、报告/章节/层级来源和 project/cwd 过滤。
- 模块三已实现 `/search`：默认搜索当前项目与真实 `cwd`，`--all-projects` 由用户显式扩大范围，并按 L0/L1 → L2 → L3 渐进式取证后生成带来源回答。

**本部分实现：**

1. **Agent 工具**：提供 `search_reports(query, scope, depth)`，复用模块三的查询、过滤、分层读取和来源格式。
2. **按需调用**：用户提到历史讨论、旧方案、项目决策或当前上下文缺少背景时，Agent 可以调用；普通问题不自动检索。
3. **范围控制**：默认只查当前项目和真实 `cwd`；`all-projects` 必须有用户的明确授权，Agent 不能自行扩大。
4. **渐进式深度**：默认从 summary 开始，必要时再请求 detail 或 conversation；每轮有调用次数、结果数量和 token 上限。
5. **受控结果**：工具返回带报告、章节、thread、project、cwd 和层级来源的资料，不直接生成无来源答案，不修改报告。
6. **故障回退**：索引不可用、无命中、报告失效或 L3 不可读时，工具返回可区分状态，Agent 仍可基于当前上下文回答并说明依据不足。

**本部分不做：**

- 每轮对话无条件预检索或隐藏式注入全部报告；
- 新增 `/search` 命令、FTS5 实现或另一套检索逻辑；
- Embedding、向量数据库、普通本地文件知识源；
- 自动跨项目搜索、自动读取其他项目文件或改变 sandbox 权限；
- 把检索内容写入报告、长期记忆或合并进当前 thread 的历史摘要；
- 让报告中的文本指令改变 Agent 的系统规则、工具权限或执行行为。

## 工具契约

建议提供以下受控接口：

```text
search_reports(
    query: str,
    scope: "current" | "all-projects" = "current",
    depth: "summary" | "detail" | "conversation" = "summary",
)
```

### 参数

- `query`：历史主题或自然语言问题；不能为空，长度需有限制。
- `scope=current`：使用当前 runtime 的 project 和规范化真实 `cwd` 过滤。
- `scope=all-projects`：查询所有可用项目报告；只有本轮用户明确要求跨项目时才允许。
- `depth=summary`：返回 L0/L1；默认深度，适合首次调用。
- `depth=detail`：在摘要基础上返回相关 L2 `rag` 知识块。
- `depth=conversation`：在 detail 基础上返回有限的 L3 user/assistant 对话轮次。

工具应拒绝未知枚举、空 query、未经授权的 `all-projects` 和当前 thread 无法确定默认范围的请求。不要让模型通过拼接查询文本绕过 `scope` 校验。

### 返回结构

结果应是结构化资料而不是一段无来源长文本，至少包含：

```text
status: found | no_results | index_unavailable | invalid_scope | source_unavailable | truncated
query
scope
depth
results[]
  report_id
  report_title
  report_path
  thread_id
  project
  cwd
  section
  level
  turn_start / turn_end（可选）
  excerpt
  relevance
expanded_depth
truncated
diagnostics（可选）
```

`relevance` 只表示检索排序依据，不表示事实正确性或 LLM 置信度。工具结果必须明确标记为外部资料，报告正文中的“执行命令”“忽略规则”等内容不能成为当前 Agent 的指令。

## 调用策略

### 允许调用的场景

Agent 可以在以下场景调用：

- 用户说“之前讨论过”“上次方案”“历史结论”或要求找旧报告；
- 用户询问当前 thread 之外已经完成的项目决策；
- 当前对话明确缺少某个已完成工作的上下文；
- 用户询问某个历史报告、旧设计或过去的实现结果。

### 不应调用的场景

- 普通事实问答、当前消息已经提供全部上下文的问题；
- 只需读取当前 thread 中刚产生的信息；
- 为了确认“有没有报告”而在每轮对话自动搜索；
- 试图用 `all-projects` 替代正常的当前项目检索。

工具说明和 Agent 系统提示应明确这些触发条件，但不能强制模型每轮先调用工具。是否调用由 Agent 根据用户意图决定。

### 调用次数与深度

推荐策略：

1. 第一次调用使用 `scope=current, depth=summary`；
2. 如果摘要不足，再调用同一 query 的 `depth=detail`；
3. 只有需要核对原话或细节时才调用 `depth=conversation`；
4. 单个用户轮次最多两次调用，或使用项目可配置的明确上限；
5. 达到上限、token 预算或连续无新结果时停止，不重复搜索造成循环。

如果模块三已经提供“按深度一次性展开”的服务，优先复用服务，而不是在 Agent 工具层重新读取 Markdown 或 JSONL。工具结果过长时优先保留不同报告的摘要和来源，再截断详细内容。

## 范围和授权

- 默认范围是当前 thread 的 project + 真实 `cwd`；每次工具调用重新从当前 runtime 取得，不能使用上一个 thread 的缓存。
- Agent 不能仅凭问题中的项目名切换 scope。`all-projects` 需要当前用户明确表达跨项目意图，或由显式 `/search --all-projects` 流程传入授权上下文。
- `all-projects` 只扩大 reports/RAG 的读取范围，不改变 thread、project、cwd、sandbox 或文件工具权限。
- 同项目名但 cwd 不同的报告不能被 current scope 命中；相似前缀路径和符号链接按模块三的规范化真实路径规则处理。
- 当前 thread 无 cwd 时，拒绝默认 scope；不能退回进程 `Path.cwd()`，不能静默改用全项目范围。

授权不能只依赖模型传入的字符串参数。工具边界应从当前运行上下文取得“用户是否明确授权跨项目”的事实；无法确认时拒绝 `all-projects`。

## 上下文和状态边界

- 工具只返回本次 Agent 运行需要的有限资料，不把完整报告、完整对话或 checkpoint 复制进上下文。
- 复用模块三的 token 预算和截断策略；返回结果必须带 `truncated` 和实际展开层级。
- 工具不修改 report Markdown、conversation 副本、FTS5 索引或当前 thread 的业务状态。
- 需要确认现有 LangGraph/tool 生命周期：工具调用及结果可能按框架默认作为 tool message 进入 checkpoint。若框架支持临时/不持久化消息，优先使用；否则限制结果大小并明确记录实际持久化行为，不能声称检索内容完全不会被保存。
- 即使 tool message 被 checkpoint 保存，也不能把它当作新的长期报告或索引来源；下一次 `/report` 是否包含该工具调用，遵循模块一已定义的消息筛选规则。
- 检索结果是资料，不是系统提示。使用明确边界标记交给模型，防止报告中的 prompt injection 改变工具选择或权限。

## 失败行为

- `no_results`：告诉 Agent 当前范围没有可靠命中，不自动扩大范围。
- `index_unavailable`：说明索引不可用，提示用户使用模块二的重建流程；工具不在当前调用中隐式重建。
- `source_unavailable`：索引命中但报告/对话副本不可读，只返回仍可验证的其他结果并标注失效来源。
- `invalid_scope`：跨项目未授权、当前 thread 无 cwd 或过滤参数无效；不执行查询。
- `truncated`：返回预算内结果，标记未返回的层级/内容；不要把截断结果表现成完整历史。
- 底层查询或读取异常：返回结构化错误和可诊断日志，不抛出未处理异常导致整个 Agent 运行失败。
- LLM 因工具结果失败：保留当前上下文和工具状态，允许 Agent 说明无法基于历史作答；不要伪造历史结论。

## 测试流程

使用临时 `storage_root`、至少两个项目和多个 thread，复用模块一、二、三的测试夹具。先运行工具单元测试，再运行 Agent graph、CLI/TUI 和跨进程回归，最后运行全量测试。

### 工具契约

- 参数校验、默认值、空 query、未知 enum、结果 schema、来源字段和 token 上限。
- current scope 只返回当前 project/cwd；all-projects 没有授权时拒绝，有明确授权时返回跨项目来源。
- 切换 thread 或项目后范围实时变化；不会使用上一个运行的 scope 或结果缓存。

### Agent 触发策略

- 使用确定性模型或工具调用替身验证历史问题可调用工具；普通问题不调用。
- 验证首次 summary、摘要不足后的 detail/conversation 展开，以及最大调用次数和无进展停止。
- 验证 Agent 不会因为报告中的文本指令调用其他工具、修改文件或扩大权限。

### 数据与持久化

- 返回结果只包含有界报告片段，不读取 tool/system checkpoint 作为深层来源。
- 工具调用是否进入 checkpoint 按实际框架断言：若持久化，大小受限且不产生新的报告/索引；若支持临时消息，确认重启后行为符合设计。
- 两个独立进程使用同一 storage root 和已有报告索引，第二进程能通过 Agent 工具检索；进程重启不丢失报告或索引。
- index 缺失、损坏、报告删除、conversation 副本损坏、LLM 失败时，状态和回退行为符合契约。

### 回归

- `/search` 与 `search_reports` 对同一 query、scope、depth 返回一致的候选和来源。
- `/report`、项目/thread 切换、sandbox、普通工具调用和 export 自动报告不回归。
- 普通对话没有隐式额外的 RAG 调用、LLM 请求或明显上下文膨胀。

最终报告实际使用的工具 schema、授权来源、调用上限、checkpoint 持久化行为、测试命令和结果，并明确说明未实现的 Embedding、混合检索和本地文件知识源。
````
