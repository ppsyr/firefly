# 附加功能 2：Embedding 与混合检索

## 本部分实现的功能（一览）

> **本部分 = "在 SQLite FTS5 的精确检索之上，为报告知识块增加语义检索，并通过混合排序提高自然语言问题的命中率"。**

本功能是后续增强，不改变报告格式、来源元数据、权限范围和 L0/L1/L2/L3 渐进式披露协议。

**前置功能已完成：**

- 模块一已实现 `/report`：thread 报告保存在用户级 `reports/`，包含 L0 概览、L1 摘要、L2 `rag` 知识块和 user/assistant-only 的 L3 对话副本。
- 模块二已实现 SQLite FTS5：报告可增量索引、全量重建，并返回章节、层级、thread、project、cwd 等来源。
- 模块三已实现 `/search`：支持当前项目/cwd、显式 `--all-projects` 和摘要到详细对话的逐层展开。
- 模块四已实现 Agent 原生 `search_reports`：Agent 仅在需要历史背景时调用，普通轮次不会无条件检索。
- 附加功能一可将普通本地文件保存为带 `source_type=local_file` 的分层报告，并将原文件保留为按需读取的快照。

**本部分实现：**

1. **Embedding 生成**：为报告的 L0/L1/L2 检索块生成向量，不默认向量化完整对话、checkpoint 或原文件快照。
2. **向量索引**：保存向量、模型/版本、维度、内容 hash 和来源元数据，支持增量更新、删除和重建。
3. **混合检索**：并行使用 FTS5 和向量相似度，合并为可解释、可调参的候选排序。
4. **统一入口**：`/search` 和 `search_reports` 复用同一混合检索服务，保持 current/all-projects 和渐进式披露语义一致。
5. **降级运行**：Embedding 模型不可用、生成失败或索引不兼容时，自动退回 FTS5，不阻断报告保存和精确搜索。
6. **效果评估**：用固定查询集比较 FTS5、Embedding 和混合排序，只有混合检索确实改善召回或自然语言问题命中时才作为默认策略。

**本部分不做：**

- 修改 `/report` 的报告生成格式和层级；
- 默认将完整 user/assistant 对话、tool/system、checkpoint 或原文件快照向量化；
- 用向量相似度替代项目/cwd 权限过滤；
- 引入向量数据库作为必需依赖；
- 因 embedding 命中自动读取其他项目文件或扩大 sandbox；
- 实时监听本地文件、自动生成文件报告或多模态 embedding；
- 每轮对话无条件生成 query embedding 或自动检索。

## 为什么保留 FTS5

Embedding 解决语义改写和同义表达，例如用户问“为什么要拆分 checkpoint”，报告写的是“按 session 隔离持久化状态”；但它不擅长精确 ID、文件名、配置键、函数名和错误码。FTS5 仍负责：

- 精确词和标识符；
- 可解释的关键词命中；
- 无模型、离线和低成本降级；
- 报告生成后无需等待外部 embedding 服务才能可搜索。

混合检索是增强，不是替换：权限过滤先执行，FTS5/向量只在允许范围内排序。

## 向量化内容和分块

默认只为这些内容生成向量：

- 报告名称和描述；
- L0 概览；
- L1 结构化摘要、关键决策、结果和待办；
- L2 `rag` 知识块。

每个向量块保留与 FTS5 相同的来源字段：

```text
embedding_id
report_id
report_path
thread_id
project
cwd
source_type
section
level
turn_start / turn_end
content_hash
embedding_model
embedding_model_version
dimension
created_at
```

块内容应保持可独立理解，必要时在生成向量前附带报告标题、章节和来源类型，但原文展示仍使用报告内容。不要把多个无关章节拼成过大的向量块，也不要把相邻块重复到无法区分来源。

### 默认不向量化

- `conversation.jsonl` 的完整对话；
- thread checkpoint、system/tool 消息和工具执行结果；
- 附加功能一的原文件 snapshot；
- 自动 export 目录中的 `final_report.md`，除非明确纳入报告索引协议。

原文件中的函数名、配置键等精确检索仍可由 FTS5 或后续 source-text 索引处理。Embedding 只索引本地文件生成的报告，不直接上传或向量化原文件全文。

## 模型、缓存和隐私

Embedding 提供者应通过已有配置机制选择，至少记录：

```text
provider
model
model_version
dimension
normalization
```

- 支持本地模型或已配置的远程服务，但不能把远程 API 当作隐含必需依赖。
- 远程 embedding 前必须有明确配置和隐私说明；失败时回退 FTS5。
- 同一 `content_hash + model + model_version + dimension` 可以复用缓存。
- 模型、维度或归一化方式变化时不能混用旧向量；应新建索引版本或重建。
- 报告内容可能包含项目源代码和内部信息，不能在没有配置许可时发送到外部服务。
- 向量缓存本身属于用户级数据，使用与 reports 相同的 `storage_root` 权限，不写入项目目录。

## 索引生命周期

### 增量生成

报告或本地文件报告成功保存后，可以为新增/变化的 L0/L1/L2 块生成 embedding。生成失败时：

- 报告和 FTS5 仍然可用；
- 该块标记为 embedding pending/failed；
- 不删除旧版本向量，除非新版本已经成功替换；
- 用户和日志能区分“没有命中”和“向量索引未完成”。

### 重建与版本切换

- 提供从报告和 FTS5 元数据重建向量索引的入口；
- 支持只重建某个模型版本、某个报告或全部报告；
- 新索引完成并校验后再切换活动版本，避免重建中查询到半成品；
- 旧索引可在失败回滚期间继续服务，完成后按策略清理；
- 报告 hash 变化时替换该报告的旧向量块；同一报告重复索引不产生重复向量。

## 混合检索

### 查询流程

```text
用户 query
   ↓
执行当前 scope/cwd 权限过滤
   ↓
FTS5 查询 ─────────┐
                    ├─ 候选去重、来源校验、分数归一化
query embedding ───┘
   ↓
混合排序
   ↓
L0/L1 → L2 → L3 渐进式披露
```

- 先执行来源范围过滤，再运行或接受 FTS5/向量结果；不能先搜全库再在展示层隐藏越权结果。
- FTS5 与向量结果按 `report_id + chunk_id` 去重。
- 两种分数需要归一化后再混合，不能直接把 BM25 和 cosine 数值相加。
- 混合权重、候选数量、最低相似度和去重规则可配置并记录在检索诊断中。
- 精确 ID、文件名、配置键等查询可提高 FTS5 权重；自然语言问题可提高向量权重，但不要只用关键词形状猜测权限。
- 即使向量命中 L2，首次注入仍优先使用对应报告的 L0/L1；需要细节时才展开 L2，之后才读取 L3。
- 结果带 FTS/向量/混合来源信息，便于评估和解释，但不把内部分数描述成事实置信度。

### 降级策略

按以下优先级运行：

1. FTS5 + Embedding 混合检索；
2. Embedding 不可用或 query embedding 失败时使用 FTS5；
3. FTS5 不可用时返回明确的索引错误，不自行读取所有报告冒充搜索；
4. 任何降级都在结果状态和日志中可识别。

`/search` 和 `search_reports` 不应因 embedding 服务临时不可用而失败，只要 FTS5 可用。普通轮次也不因为启用该功能就自动生成 query embedding。

## 权限和范围

- current scope 使用当前 thread 的 project + 规范化真实 `cwd`；all-projects 仍需用户显式授权。
- 向量索引的来源字段必须与 FTS5 一致；向量相似度不能绕过 cwd、项目和 source_type 过滤。
- thread 切换、项目切换后重新计算 scope，不能复用前一个 thread 的候选缓存。
- 附加功能一的本地文件报告遵循相同范围；向量命中只允许读取该报告或其已授权 snapshot。
- 不因向量结果显示了外部路径就读取该路径；深层来源读取仍通过现有报告/source resolver 和安全校验。

## 评估标准

Embedding 不是因为“能运行”就完成。准备固定查询集，至少包含：

- 报告中使用原词的精确关键词；
- 同义改写和自然语言问题；
- 中文、英文和中英混合问题；
- thread ID、文件名、函数名、配置键和错误码；
- 无关问题和跨项目隔离问题。

对每个查询比较：

```text
FTS5 top-k
Embedding top-k
Hybrid top-k
是否命中正确报告/块
MRR / Recall@k（可按项目规模选择）
端到端延迟
embedding 调用次数和成本
注入 token 数
来源和权限错误数
```

默认启用混合检索前，至少证明它在自然语言/同义改写集合上改善召回，同时不明显降低精确标识符查询和权限隔离。效果不稳定时保留 FTS5 作为默认，Embedding 以显式配置或实验开关启用。

---

## 提示词

````text
# 开发任务：实现 Embedding 与 FTS5 混合检索

你是这个 Python Agent 项目的开发 Agent。请先阅读实际代码，再直接实施并验证本附加功能，不要只提交方案。本功能是 Report Retrieval 的后续增强：在已存在的 SQLite FTS5 上增加可选 Embedding，并用混合排序改善语义查询。不要创建第二套报告或权限系统。

## 已完成的前置功能

1. **模块一：报告生成与持久化**
   - `/report [标题]` 将 thread 保存到用户级 `{storage_root}/reports/YYYY/MM/DD/`；
   - Markdown 有来源元信息、L0 概览、L1 结构化/逐轮摘要和 L2 `rag` 知识块；
   - `conversation.jsonl` 仅保存 user/assistant 文本，完整 checkpoint 仍保留；
   - export 自动 `final_report.md` 继续按原逻辑保存。
2. **模块二：SQLite FTS5**
   - 已索引报告的标题、描述、L0/L1/L2；
   - 已支持来源字段、project/cwd 过滤、增量更新、去重和重建。
3. **模块三：`/search`**
   - 已支持关键词/问题查询、默认 current scope、显式 `--all-projects`；
   - 已按 L0/L1 → L2 → L3 渐进式读取并生成带来源回答。
4. **模块四：Agent 原生 `search_reports`**
   - Agent 可以在需要历史背景时调用，普通轮次不自动检索；
   - 已有限制 scope、深度、调用次数和 token 的规则。
5. **附加功能一：普通本地文件知识源**
   - 已能显式导入文件并生成 `source_type=local_file` 的分层报告；
   - 已保存原文件 snapshot，报告进入统一 FTS5，原文按需展开。

请用源码核对实际接口和索引格式。优先扩展模块二的索引抽象、模块三/四的查询入口和现有配置；不要绕过范围过滤，也不要直接把原始文件或 checkpoint 全部向量化。

## 目标与边界

为报告检索块提供可选 Embedding，并与 FTS5 结果混合排序。默认向量化 thread/local-file 报告的 L0/L1/L2，不向量化完整 conversation、checkpoint、tool/system 或原文件 snapshot。

必须交付：

- Embedding provider/model 配置和能力检测；
- 按 `content_hash + model/version + dimension + normalization` 缓存向量；
- 向量块来源元数据、增量更新、模型版本切换和全量重建；
- query embedding、FTS5 + 向量候选去重、分数归一化和混合排序；
- `/search` 与 `search_reports` 共用混合检索和降级策略；
- Embedding 失败或未配置时可靠回退 FTS5；
- 固定查询集上的 FTS5/Embedding/Hybrid 评估和测试报告。

不改变报告层级、来源格式、project/cwd 权限、sandbox、`/report`、thread/checkpoint 或本地文件导入语义。不实现实时监听、多模态、自动全库 embedding 或向量数据库必需依赖。

## 开始前源码核查

先简要确认并继续开发：

1. 模块二 FTS5 的 chunk、metadata、来源过滤和索引生命周期接口；
2. 模块三/四的查询参数、逐层披露和 token 预算；
3. thread/local-file 报告的 source_type、hash、version 和报告路径字段；
4. 项目已有配置、secret 管理、网络调用、重试和缓存约定；
5. Python 运行环境是否已有可用 embedding 库/本地模型，若没有，如何提供可测试的 provider 抽象和确定性替身；
6. SQLite 是否适合保存向量 blob，或是否需要一个可选、可重建的本地向量索引。第一版不要未经评估引入大型向量数据库。

## Embedding 数据契约

- 只处理可检索的报告 L0/L1/L2 块。每个向量必须关联 report_id、chunk_id、source_type、thread/project/cwd、section、level、content_hash、model、model_version、dimension 和生成时间。
- 同一内容 hash、模型、版本、维度和归一化方式可以复用缓存；任一参数变化都视为不同向量空间，不能混算。
- thread 的 conversation、完整 checkpoint、tool/system、自动 export artifact 和本地文件 snapshot 默认不生成向量。文件报告的摘要可以生成向量，但原文只能作为后续精确/深层来源。
- 向量索引是派生数据。报告/FTS5 仍可单独工作，向量丢失可重建；不能把向量库作为唯一报告存储。

## Provider、隐私和成本

- 通过已有配置选择本地或远程 provider；未配置、不可用或失败时自动走 FTS5。
- 远程 provider 必须是显式配置；在发送报告内容前检查隐私/联网设置，不得默默上传源代码或内部报告。
- 设置批量大小、超时、重试、速率和单次 token/字符上限；失败块进入 pending/failed 状态，不能阻塞报告保存。
- 模型和维度升级使用新索引版本，完成后原子切换；重建中继续使用旧版本或 FTS5。
- 缓存和向量文件存储在用户级 storage root，不写入项目目录；记录 provider/model 便于删除和审计。

## 混合排序

1. 先根据当前 runtime 的 project/cwd/scope 过滤可见元数据；
2. 对同一 query 执行 FTS5 和（如可用）query embedding；
3. 将 BM25/FTS 与 cosine 或 provider 相似度分别归一化；
4. 按配置权重、精确标识符规则和去重规则合并候选；
5. 返回稳定的 chunk/report 来源和排序诊断；
6. 交给模块三/四执行 L0/L1 → L2 → L3 渐进式披露。

不能直接相加未经归一化的两个分数，也不能先全库向量搜索再在展示层隐藏越权结果。精确 thread ID、文件名、函数名、配置键和错误码可提高 FTS5 权重；语义问题可提高向量权重，但权重不得改变权限过滤。

## 降级和更新

- FTS5 + Embedding：正常混合模式；
- Embedding 未配置、服务失败、模型不兼容或 query embedding 失败：只使用 FTS5，并返回可识别的降级状态；
- FTS5 也不可用：返回索引错误，不扫描所有报告冒充检索；
- 报告 hash 变化：替换对应版本的 FTS/向量块；旧版本按报告版本策略处理；
- 重建失败：保留旧活动索引，不切换到半成品；允许重试和全量重建。

普通对话不因为开启 Embedding 就每轮生成 query 向量；只有 `/search` 或 Agent 实际调用报告检索时才产生查询 embedding。

## 评估与测试

使用临时 storage root、至少两个项目和 thread/local-file 报告。先运行 provider/cache、向量生命周期和混合排序测试，再运行 `/search`、Agent、权限、跨进程和全量回归。

准备固定查询集，覆盖精确关键词、同义改写、中文、英文、中英混合、thread ID、文件名/函数名/配置键、无关问题和跨项目问题。对 FTS5、Embedding 和 Hybrid 记录命中正确报告/块、Recall@k 或 MRR、延迟、embedding 次数、注入 token、降级状态和权限错误。

至少验证：

1. 相同内容和模型参数命中缓存；hash/model/version/dimension 变化触发新向量；
2. FTS5、Embedding、Hybrid 的候选去重、分数归一化、稳定排序和来源正确；
3. L2 命中仍先返回对应 L0/L1，必要时才展开 L2/L3；原始对话和文件 snapshot 不被默认向量化；
4. current/all-projects、项目切换、thread 切换、符号链接和相似路径不能越权；
5. 本地/远程 provider 不可用、超时、限流、隐私禁用、索引损坏和重建失败都回退或报错明确；
6. 两个独立进程能读取同一活动索引；模型升级或重建不会让查询读到半成品；
7. 普通对话没有隐式 embedding 调用，`/search` 和 `search_reports` 对同一配置返回一致候选和来源；
8. 运行评估后说明是否值得默认启用 Hybrid；如果没有改善，保留 FTS5 默认并把 Embedding 作为显式实验开关。

最终报告实际使用的 provider、存储格式、缓存键、混合权重、降级规则、隐私配置、评估结果和测试命令；明确说明未实现的实时文件监听、多模态和其他向量数据库适配。
````
