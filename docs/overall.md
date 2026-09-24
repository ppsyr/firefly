# 组件实现


## 一、先建立一张“组件地图”

从 `bootstrap_runtime` 出发，组件分成五层：

```
┌─────────────────────────────────────────────┐
│  1. 入口层                                   │
│     CLI / TUI / run                         │
└─────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────┐
│  2. 装配层                                   │
│     bootstrap_runtime                       │
│     make_lead_agent                         │
└─────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────┐
│  3. 执行层                                   │
│     create_agent → graph                    │
│     LeaderAgent                             │
└─────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────┐
│  4. 组件层.                                  │
│     - tools.                                │
│     - context_engineering.                  │
│     - middlewares.                          │
│     - memory.                               │
│     - skill.                                │
│     - sandbox.                              │
│     - multiagent.                           │
│     - reporting.                            │
└─────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────┐
│  5. 支撑层                                   │
│     config / prompts / runtime / state      │
│     observability / journal                 │
└─────────────────────────────────────────────┘
```

---

## 二、一次对话的生命周期

### 第 1 站：`middlewares/`（中间件）

**为什么先看**：中间件是**串起所有组件的胶水**。看懂中间件，就知道每个组件在什么时候被调用、按什么顺序被调用、通过什么机制传递数据。

---

#### 1. 先建立「钩子 + state」的心智模型

LangGraph 的 AgentMiddleware 提供 6 类钩子，分成两组：

**state-channel hooks（返回 dict，写 state）**

| 钩子 | 触发时机 | 能做什么 |
|---|---|---|
| `before_agent` | 一次 run 开始前 | 初始化、清理旧状态 |
| `after_agent` | 一次 run 结束后 | 收口、清理、生成最终产物 |
| `before_model` | 每次模型调用前 | 注入消息、改 state、可 `jump_to` |
| `after_model` | 每次模型调用后 | 检查结果、可 `jump_to` 回 model |

**wrap hooks（包住 handler，只能改 request/result）**

| 钩子 | 触发时机 | 能做什么 |
|---|---|---|
| `wrap_model_call` | 包住「模型调用」 | 改 request（PRE） |
| `wrap_tool_call` | 包住「工具调用」 | 改 request / result（PRE + POST） |

**关键差异**：

- state-channel hooks 返回 `dict`，由框架合并进 state；
- wrap hooks 返回 `ModelCallResult` / `ToolMessage` / `Command`，**没有 state 通道**，所以状态写入只能靠 before/after hook。

**洋葱结构**：wrap hooks 是嵌套的。注册顺序 `[A, B, C]` 时：

```
A.wrap_model_call → B.wrap_model_call → C.wrap_model_call → handler
```

A 在最外层，C 在最内层；返回时反向。

**执行方向**：

- `before_agent` / `before_model` / `after_model`：按注册顺序**正序**；
- `after_agent`：按注册顺序**反序**（收口）；
- `wrap_model_call` / `wrap_tool_call`：注册顺序**由外到内**。

---

#### 2. 看什么

按「一次 run 的生命周期」顺序看，每个阶段挑代表中间件：

**阶段 ①：run 开始前**

| 文件 | 看什么 |
|---|---|
| `run_journal_middleware.py` | `before_agent` 怎么写 "agent.started"；`_get_runtime_value` 怎么从 runtime 多路径取值（这是全仓库复用的基础设施） |
| `tool_call_middleware.py` | `before_agent` 怎么记录 errors baseline，为 per-run 计数打基础 |

**阶段 ②：每次模型调用前**

| 文件 | 看什么 |
|---|---|
| `system_context_middleware.py` | 最简示例：`before_model` 返回 dict 写 metadata |
| `tagged_context_middleware.py` | `wrap_model_call` 怎么渲染 XML 标签序列 + `request.override` 怎么用 |
| `message_normalizer_middleware.py` | `wrap_model_call` 怎么合并 SystemMessage（洋葱内层的典型职责） |
| `skill_injection_middleware.py` | `before_model` 怎么注入 SystemMessage + 写 provenance 锚点 + 发 custom event |
| `memory_recall_middleware.py` | `before_model` 怎么注入 per-call HumanMessage（保护 prompt caching） |
| `todo_middleware.py` | `before_model` 双阈值 Nag + `after_model` 完成度强制 + `wrap_model_call` 注入提醒（三层保护） |
| `dangling_tool_call_middleware.py` | `before_model` 怎么修补悬空 tool_call，防 400 |

**阶段 ③：每次模型调用后**

| 文件 | 看什么 |
|---|---|
| `reflection_middleware.py` | `after_model` 怎么判断「模型想退出」+ `jump_to="model"` 怎么用 + jump 预算 |
| `stall_detection_middleware.py` | `after_model` 怎么用 `Command(goto=END)` 暂停 graph |
| `todo_middleware.py` | `after_model` 完成度强制（与 Reflection 共享 jump 预算） |

**阶段 ④：工具调用**

| 文件 | 看什么 |
|---|---|
| `help_request_middleware.py` | `wrap_tool_call` 怎么拦截 ask_help + 返回 `Command(goto=END)` |
| `evidence_middleware.py` | `wrap_tool_call` 怎么双写（messages + observations/sources） |
| `tool_call_middleware.py` | `wrap_tool_call` 怎么记账本 + 重试预算 + 硬预算 |
| `run_journal_middleware.py` | `wrap_tool_call` 怎么记录 tool.called / tool.finished |
| `sandbox_middleware.py` | `wrap_tool_call` 怎么按需 acquire + present_files 落产出物 |
| `skill_metrics_middleware.py` | `wrap_tool_call` 怎么读 ContextVar 做 applied 打点 |

**阶段 ⑤：run 结束后**

| 文件 | 看什么 |
|---|---|
| `title_middleware.py` | 最简示例：`after_agent` 返回 dict 写 metadata.title |
| `report_middleware.py` | `after_agent` 怎么独立合成 final_report |
| `skill_metrics_middleware.py` | `after_agent` 怎么做结果归因 |

---

#### 3. 重点看

**3.1. 四个钩子各在什么时候跑**

| 钩子 | 频率 | 典型用途 |
|---|---|---|
| `before_agent` | 一次 run 一次 | 初始化、清理旧 run 状态 |
| `before_model` | 每次模型调用一次 | 注入消息、写 state |
| `wrap_model_call` | 每次模型调用一次 | 改 request（渲染、合并） |
| `after_model` | 每次模型调用一次 | 检查、可 jump 回 model |
| `wrap_tool_call` | 每次工具调用一次 | 拦截、审计、记账 |
| `after_agent` | 一次 run 一次 | 收口、生成最终产物 |

**3.2. 中间件之间怎么通过 state 传递数据**

三条常见通道：

- **直接写 state 字段**：`{"metadata": {...}}`、`{"errors": [...]}`、`{"observations": [...]}`；
- **ContextVar**：`_active_skills_ctx` / `_applied_ctx` / `set_current_state`，用于 wrap hook 拿不到 state 时桥接；
- **延迟队列**：`_pending_completion_reminders` / `_pending_summaries`，在 wrap_tool_call 入队、before_model 出队，避免在并行 tool_calls 之间插 HumanMessage 破坏配对。

**3.3. `Command(goto=END)` 这种控制流怎么用**

两种跳转：

- `Command(goto=END)`：直接结束本次 run，把控制权交回用户。用于暂停求助（HelpRequest / StallDetection）；
- `{"jump_to": "model"}`：跳回 model 节点，让模型继续跑。用于完成度强制（Todo）和充分性反思（Reflection）。

`jump_to` 需要在 hook 上标 `@hook_config(can_jump_to=["model"])` 才生效。

**3.4. 共享预算机制**

`_jump_budget` 让 Todo 和 Reflection 的 jump 合计 ≤3，防止两个中间件互相触发导致无限跳转。这是「多个中间件争抢同一资源」的典型解法。

**3.5. 条件挂载**

不是所有中间件都总是挂载。`_build_middlewares` 里有条件判断：

- `context_governance is not None` → 治理层 3 个；
- `skill_manager` 存在 → SkillInjection + SkillMetrics + SkillActivation；
- `sandbox_provider is not None` → Sandbox；
- `memory_provider is not None` → Memory；
- `memory_worker is not None` → MemoryConsolidation；
- `orchestration_middleware is not None` → Orchestration；
- `expert_mode` → Todo 强制完成 + Reflection 充分性策略；
- `model is not None` → Report。

---

#### 4. 不要看

- **每个中间件的业务细节**（如 skill 怎么选、证据怎么抽、报告 prompt 怎么设计）——先建立「钩子 + state」的心智模型，业务细节留到对应组件层再看。
- **已移除的 `LoopDetectionMiddleware`**——它不在 `_build_middlewares` 里，可以跳过。
- **no-op 的 `SummarizationMiddleware`**——V1 占位，没有逻辑。

---

#### 5. 看完这一站应该能回答

1. 一次 run 从开始到结束，6 类钩子各触发几次？
2. `before_model` 和 `wrap_model_call` 谁先跑？为什么？
3. `after_agent` 为什么是反序？
4. 哪些中间件会改 `request.messages`？哪些只写 state？
5. `Command(goto=END)` 和 `{"jump_to": "model"}` 分别用在什么场景？
6. 两个中间件想共享一个资源（如 jump 次数）时，怎么协调？
7. 为什么有些中间件只实现 async 版本？

---

### 第 2 站：`context_engineering/`（上下文工程）

**为什么第二**：上下文是**最影响 Agent 行为**的组件——**预算、压缩、外化、快照**都在这里。看懂这一站，就知道「模型每次看到的上下文是怎么被算出来、被裁剪、被渲染」的。

---

#### 1. 先建立「治理层 + 策略 bundle + 6 hook」的心智模型

`context_engineering` 不是「一堆中间件」，而是**一套治理框架**：框架只提供契约和装配，具体治理逻辑由「策略 bundle」实现。

**三层结构**：

```
装配层（builder.py）
    └─ build_governance_middlewares(config, model, summarize_model) → list
         ├─ 固定挂公共 2：TaggedContextMiddleware → MessageNormalizerMiddleware
         └─ 按 config.strategy 查 registry，挂 StrategyMiddleware(bundle)
              │
接入层（contract.py + registry.py + strategy_middleware.py）
    ├─ contract.py          ：定义 GovernanceStrategy 6 hook + Context/Result/Metric
    ├─ registry.py          ：name → bundle_cls 映射，@register_strategy 注册
    └─ strategy_middleware.py：adapter，把 6 hook 路由到 bundle
              │
策略层（strategies/default/）
    └─ DefaultStrategy（@register_strategy("default")）
         ├─ budget.py       ：BudgetTrackerExecutor（算 fraction / pending）
         ├─ externalizer.py ：ExternalizerExecutor（外化超长 tool result）
         ├─ snapshot.py     ：SnapshotExecutor（P4 压缩前存快照）
         └─ summarizer.py   ：SummarizerExecutor（P4 用 LLM 摘要）
```

**关键差异**：

- **装配层**只决定「挂哪几个 middleware」，不含治理逻辑；
- **接入层**只做「契约转换 + handler 包裹」，不含治理逻辑；
- **策略层**才是真正做决策的地方（什么时候压缩、压什么、怎么压）。

**6 hook 与两层处理**：

`GovernanceStrategy` 定义 6 对 hook（sync + async），分两类处理：

| 类别 | hook | 返回 | 状态写入 |
|---|---|---|---|
| state-channel | before/after_agent、before/after_model | `GovernanceResult` → `apply_governance_result` → dict patch | 持久写 ThreadState |
| wrap | wrap_model_call、wrap_tool_call | `GovernanceResult.request_override` | 只改 request/result，不持久 |

**执行方向**：

- `before_agent` / `before_model` / `after_model`：按挂载顺序**正序**；
- `after_agent`：按挂载顺序**反序**（收口）；
- `wrap_model_call` / `wrap_tool_call`：挂载顺序**由外到内**（洋葱结构）。

**公共 2 + 策略 1 的挂载顺序**：

```
TaggedContextMiddleware（最外层）→ MessageNormalizerMiddleware → StrategyMiddleware（最内层）
```

在 `wrap_model_call` 链上：

```
TaggedContext（渲染 XML 标签）→ MessageNormalizer（合并 SystemMessage）→ Strategy（策略 request_override）
```

---

#### 2. 看什么

按「一次治理决策的生命周期」顺序看，每个文件挑重点：

**装配与契约层**

| 文件 | 看什么 |
|---|---|
| `builder.py` | `build_governance_middlewares` 怎么组装公共 2 + 策略 1；策略未注册时怎么降级 |
| `contract.py` | `GovernanceStrategy` 6 hook 的签名；`GovernanceContext` / `GovernanceResult` / `GovernanceMetric` 字段；`apply_governance_result` 怎么把 result 转 dict patch |
| `registry.py` | `@register_strategy(name)` 注册；`get_strategy_class(name)` 查表；重注册告警 |
| `strategy_middleware.py` | `StrategyMiddleware._ctx` 怎么构造 GovernanceContext；state-channel hook 走 `apply_governance_result`；wrap hook 怎么包 handler |

**公共 2 中间件**

| 文件 | 看什么 |
|---|---|
| `middlewares/tagged_context_middleware.py` | `ContextAssembler.render_context_block` 渲染 `<goal><plan><reflection><summary><date>`；`render_messages_for_llm` 怎么改写 AIMessage content；`after_model` 怎么写 `state.tagged_context` trace |
| `middlewares/message_normalizer_middleware.py` | `_coalesce` 怎么合并多 SystemMessage 为单条 leading；为什么只动 request 不动 state |

**策略层（DefaultStrategy）**

| 文件 | 看什么 |
|---|---|
| `strategies/default/strategy.py` | 6 hook 的编排：before/after_agent 做 init/clear；before_model 做 P3/P4/P1 压缩；after_model 做 track + P5；wrap_tool_call 做单条外化 |
| `strategies/default/budget.py` | `init_budget` / `track` / `clear_run_state`；fraction 怎么算；pending 怎么标 |
| `strategies/default/externalizer.py` | `externalize_if_needed`（单条）vs `externalize_history`（批量 FIFO + 近 N 轮豁免 + 每轮保 1） |
| `strategies/default/snapshot.py` | P4 压缩前存快照；`snapshot_if_pending` 怎么判 P4 |
| `strategies/default/summarizer.py` | `summarize_if_pending` 怎么切分 to_summarize/preserved；`_snap_to_pairing` / `_strip_orphan_tools` 怎么保 pairing；`_call_llm` 怎么调摘要模型 |
| `strategies/default/_constants.py` | `CST` 时区常量 |

---

#### 3. 重点看

##### 3.1. 5 个阶段（P1/P2/P3/P4/P5）各在什么时候触发

阈值定义在 `strategy.py` 的 `_DEFAULT_THRESHOLDS`，可被 `params.thresholds` 覆盖：

| 阶段 | 阈值 | 触发点 | 动作 |
|---|---|---|---|
| P1 | fraction ≥ 0.40 | `before_model` | `externalize_history`（批量外化旧 tool result） |
| P2 | fraction ≥ 0.50 | `after_model` | `_mark_thinking`（给 AIMessage 打 POIROT_THINKING 标记） |
| P3 | fraction ≥ 0.60 | `before_model` | 设 `p3_obs_limit`（当前渲染层未消费，no-op） |
| P4 | fraction ≥ 0.80 | `before_model` | snapshot → summarizer（全量摘要） |
| P5 | fraction ≥ 0.90 | `after_model` | `_strip_tool_calls` + 收尾提示 + `jump_to="model"` |
| hard_stop | ≥ 0.99 | P5 分支内 | 文案区分（99% 硬底线） |

**pending 列表由 `budget.track` 在 after_model 标**，下一轮 before_model 读它决定执行哪个分支。

##### 3.2. `budget.fraction` 是怎么算出来的

`BudgetTrackerExecutor.track`：

1. 遍历 messages 里的 AIMessage，读 `usage_metadata.input_tokens/output_tokens`；
2. 与 `seen_msgs[msg.id]` 上次值做 diff，只把增量累加进 `budget.input/output`；
3. `current = token_counter(messages)`；
4. `fraction = current / window`（window 由 `resolve_window_size(model)` 解析，config.window 可覆盖）；
5. 按阈值填 `pending`。

##### 3.3. 三个通道的数据传递

- **state 字段**：`governance.default.{budget, pending, summary, snapshot_path, warned, ...}`；
- **messages_patch**：pairing 补全、P4 摘要替换、P1 外化替换、thinking 标记、P5 剥 tool_calls；
- **request_override**：`wrap_tool_call` 外化成功时替换 ToolMessage。

##### 3.4. `jump_to="model"` 和 `@hook_config(can_jump_to=["model"])`

- `before_model` / `after_model` 带 `@hook_config(can_jump_to=["model"])`；
- DefaultStrategy 实际只有 **after_model 的 P5 分支**用 `jump_to="model"`；
- before_model 不 jump（P4/P1 分支都不设）；
- `warned` 标志保证 P5 每 run 只触发一次，防死循环。

##### 3.5. pairing 保护贯穿全流程

- `before_model._ensure_pairing`：补缺失 ToolMessage；
- `summarizer._snap_to_pairing`：切分点不落在 ToolMessage 上；
- `summarizer._strip_orphan_tools`：preserved 段孤立 tool/ai 消息移到 to_summarize；
- `after_model._strip_tool_calls`：剥 tool_calls 时复用原 id（add_messages 替换，非追加）。

##### 3.6. 外化 / 快照落盘位置

- 外化：`.poirot/externalized/<tool_name>-<id>.{json,txt}`
- 快照：`.poirot/snapshots/snapshot-<ts>.json`
- trace：`.poirot/logs/threads/<tid>/runs/<rid>/compaction.jsonl`

##### 3.7. 三个中间件的分工

| 中间件 | 介入 hook | 作用面 | 是否持久 |
|---|---|---|---|
| TaggedContextMiddleware | wrap_model_call + after_model | 渲染 XML 标签上下文 | request-scoped；state 只存 trace |
| MessageNormalizerMiddleware | wrap_model_call | 合并多 SystemMessage 为单条 leading | request-scoped，不动 state |
| StrategyMiddleware | 6 hook 全量 | 策略路由（压缩/预算/外化/快照/摘要） | state_patch 持久 |

##### 3.8. 条件挂载

`context_governance is not None` → 挂治理层 3 个；否则完全不挂。

---

#### 4. 不要看

- **每个 Executor 的日志/诊断细节**（如 `externalize_history` 里那一堆 `logger.info`）——先建立「阶段 → Executor → state 读写」的心智模型。
- **`_DEFAULT_THRESHOLDS` 之外的参数微调**（`exempt_rounds` / `preview_chars` / `preserve_recent`）——知道有这些参数即可，具体值留到调优时再看。
- **`GovernanceMetric` 和 `merge_metrics_into_governance`**——contract 提供了，但 DefaultStrategy **实际不用**，计数直接写 `governance.default.metrics`。
- **`MemorySink` Protocol**——契约预留，DefaultStrategy 当前不调 flush。
- **`utilities.py` 的 `_MODEL_WINDOW_MAP` 全表**——知道它存在、知道会穿透 FallbackChatModel 即可，具体模型窗口留到需要时查。

---

#### 5. 看完这一站应该能回答

1. `build_governance_middlewares` 返回的 list 里有哪几个 middleware？顺序是什么？
2. 策略 bundle 未注册时会发生什么？为什么不抛异常？
3. `GovernanceContext` 和 `GovernanceResult` 各有哪些字段？分别谁用？
4. `apply_governance_result` 把 result 转成什么？wrap hook 为什么不走它？
5. `budget.fraction` 是怎么算出来的？`pending` 是谁标的？
6. P1/P2/P3/P4/P5 各在哪个 hook 触发？各做什么？
7. P4 分支里 snapshot 和 summarizer 的先后顺序是什么？为什么？
8. `_ensure_pairing` / `_snap_to_pairing` / `_strip_orphan_tools` / `_strip_tool_calls` 分别在防什么？
9. `jump_to="model"` 只在哪个分支用？`warned` 标志防什么？
10. TaggedContext 和 MessageNormalizer 在 `wrap_model_call` 链上的先后顺序是什么？各自改 request 的哪部分？
11. 外化 / 快照 / trace 分别落在哪个目录？
12. `wrap_tool_call` 在 DefaultStrategy 下做什么？`wrap_model_call` 呢？

---

#### 6. 对照

- 你在 `stream_service.py` 里看到的 `budget_update` / `compaction_start` / `compaction_end` 事件，就是 `strategy.py._emit_trace` / `_emit_compaction_event` 发的。
- 你在 `StrategyMiddleware` 里看到的 6 hook 路由，对应 `contract.py` 的 `GovernanceStrategy` Protocol。
- 你在 `state.tagged_context` 里看到的渲染快照，来自 `TaggedContextMiddleware.after_model`。
- 你在 `governance.default` 里看到的 `budget` / `pending` / `summary` / `snapshot_path` / `warned`，全部由 DefaultStrategy 的 4 个 Executor 写。


### 第 3 站：`agent_tools/`（工具）

**为什么第三**：工具是 Agent 的“手”，也是**唯一由 LLM 主动发起调用**的组件。中间件决定「工具调用前后发生什么」，上下文工程决定「工具结果如何被裁剪」，而 `agent_tools` 决定「LLM 到底能调哪些工具、长什么样、结果怎么回流」。

---

#### 1. 先建立「三层 + 三来源 + 两通道」的心智模型

**三层结构**：

```
定义层（builtin/*.py）
    └─ 每个文件一个 @tool，只做一件事、只返回字符串、不抛异常

汇总层（builtin/__init__.py）
    └─ get_builtin_tools()  → 只收集，不去重

装配层（available.py）
    └─ get_available_tools(groups, include_mcp)
         ├─ get_builtin_tools() → dedupe_by_name()
         └─ 按 _tool_group(t.name) 做 group 过滤
```

**三个来源（并行注入，彼此独立）**：

| 来源 | 加载路径 | 归谁管 | 走 available.py |
|---|---|---|---|
| builtin | `get_builtin_tools()` | 本包 | ✅ |
| MCP | `build_mcp_manager()` | McpManager | ❌ |
| sandbox | `make_sandbox_tools(provider)` | Stage 4 bootstrap | ❌ |

**两个通道（结果回流）**：

- **成功**：返回字符串 → `ToolMessage` → 回 messages；
- **失败**：不抛异常，**转 JSON error 字符串**回流，让 LLM 自行决策。

**关键差异**：定义层只管单个工具实现；汇总层只管收集；装配层才做去重与裁剪。三来源最终都汇成 `list[BaseTool]`，但加载路径独立，运行时通过**工具名 / metadata** 耦合。

---

#### 2. 看什么

**定义层（builtin/*.py）**

| 文件 | 看什么 |
|---|---|
| `ddg_search.py` | `@tool("web_search", parse_docstring=True)`；3 类失败都转 JSON error；结果规范化 `title/url/content` |
| `read_snapshot.py` | 最简示例：`@tool` 不带名（工具名 = 函数名）；`OSError` 转错误串 |
| `ask_help.py` | 半跨界案例：`@tool("ask_help", return_direct=True)` 是空壳，拦截在 `HelpRequestMiddleware`；`Literal` 限定 4 种类型 |
| `skill_search.py` | 双源合并；`ImportError` 静默降级 / `Exception` 打 warning；`SkillMeta` → dict |

**汇总层**

| 文件 | 看什么 |
|---|---|
| `builtin/__init__.py` | `get_builtin_tools()` 返回 4 个工具；为什么不去重、不含 sandbox/MCP |

**装配层**

| 文件 | 看什么 |
|---|---|
| `available.py` | `_tool_group` 三分类；`dedupe_by_name`；`get_available_tools` 两条分支 |

**旁支**

| 文件 | 看什么 |
|---|---|
| `mcp_metadata.py` | `tag_mcp_tool` 写 `poirot_mcp` 标记（in-place）；`is_mcp_tool` 判定；None 兜底 |

---

#### 3. 重点看

**3.1. 函数 → `BaseTool`**

| 装饰方式 | 工具名 |
|---|---|
| `@tool` | 函数名 |
| `@tool("web_search")` | 显式名（≠ 函数名） |
| `@tool("ask_help", return_direct=True)` | 显式名 + 直接返回（配 middleware 拦截） |
| `@tool("web_search", parse_docstring=True)` | 显式名 + docstring 的 `Args:` 给模型 |

`args_schema` 由签名推导；`Literal[...]` 变枚举约束。

**3.2. 三来源边界**

| 来源 | 关键约束 |
|---|---|
| builtin | 走 group 过滤；同名优先 builtin |
| MCP | `mcp_metadata` 打标区分；`include_mcp` 是保留参数，无加载逻辑 |
| sandbox | `SANDBOX_TOOL_NAMES` 仅用于分类，实际由 bootstrap 注入 |

**3.3. group 三分类**

| group | 加载模式 |
|---|---|
| `core` | default + expert 都加载（省上下文） |
| `sandbox` | 仅分类判定，实际 bootstrap 注入 |
| `deferred` | 仅 expert 加载 |

`get_available_tools`：`groups=None` → 全部；指定 → 按 `_tool_group` 过滤 + info 日志。

**3.4. 去重与优先级**

- `dedupe_by_name`：按 `tool.name` 去重，保留首次，顺序不变；
- **同名优先 builtin**（约定），本层不做跨来源去重。

**3.5. 失败统一模式**

所有 builtin 工具：**不抛异常 + 转错误串**。

| 工具 | 失败分支 | 返回 |
|---|---|---|
| `web_search` | 未装 / 异常 / 无结果 | `{"error","query"}` |
| `read_snapshot` | `OSError` | `"读取快照失败：{path}"` |
| `skill_search` | hub 异常 | 降级只搜 builtin |
| `ask_help` | —（空壳） | 占位字符串 |

目的：让 LLM 感知失败并自行决策，不打断 graph。

**3.6. 结果回流**

```
LLM 调用工具 → tools 节点执行 → 返回字符串 / error JSON
    → 包成 ToolMessage → 回 messages → 下一轮 model
```

`ask_help` 例外：`return_direct=True` + middleware 拦截 → `Command(goto=END)` 暂停。

**3.7. 与中间件的唯一耦合点**

| 耦合点 | 机制 |
|---|---|
| `SkillMetricsMiddleware` | 用 `tool_name in allowed_tools` 判定 |
| `HelpRequestMiddleware` | 拦截工具名 `ask_help` |
| MCP 分流 | 用 `is_mcp_tool` 按 metadata 区分 |

静态独立，运行时靠**工具名 / metadata** 耦合。

---

#### 4. 不要看

- 工具的库调用细节（ddgs 参数等）——留到调优时再看；
- `select_search_tool` 的调用方——预留/辅助，builtin 链路不依赖；
- `include_mcp` 的扩展实现——当前无加载逻辑；
- `SANDBOX_TOOL_NAMES` 的实际加载——只在 bootstrap。

---

#### 5. 看完这一站应该能回答

1. `@tool` 装饰后，函数怎么变 `BaseTool`？工具名从哪来？
2. `@tool("web_search")` 和 `@tool` 的区别？`parse_docstring=True` 做了什么？
3. `get_builtin_tools()` 返回哪几个？为什么不去重？
4. `_tool_group` 三个分类的加载模式？
5. `get_available_tools(groups=None)` 和 `groups=["core"]` 分别返回什么？
6. 三来源各由谁加载？为什么互相独立？
7. builtin 工具失败会怎样？为什么都不抛异常？
8. `ask_help` 为什么是空壳？谁接管执行？
9. 同名去重优先保留谁？在哪层做？
10. `agent_tools` 与 middleware 有代码依赖吗？运行时怎么耦合？

---

#### 6. 对照

- `available.py` 的 `CORE_TOOL_NAMES` / `SANDBOX_TOOL_NAMES` ↔ group 三分类；
- `builtin/__init__.py` 的 `get_builtin_tools()` ↔ `get_available_tools` 的唯一 builtin 来源；
- `skill_metrics_middleware.py` 的 `tool_name in allowed_tools` ↔ 本层工具对象的 `name`；
- `help_request_middleware.py` 的 `ask_help` 拦截 ↔ 本层 `ask_help_tool`；
- `mcp_metadata.py` 的 `poirot_mcp` 标记 ↔ 中间件区分 MCP 来源的依据；
- `builtin/skill_search.py` 的 JSON 返回 ↔ 被上下文工程层纳入预算与裁剪。
### 第 4 站：`memory/`（记忆）

**为什么第四**：前三站都是「一次 run 内」的事（流程、预算、执行）；memory 管的是「**这次 run 之外**」——上一轮说过的话、沉淀的知识、该忘的东西。看懂它，才知道多轮对话里「记忆从哪来、什么时候进 prompt、什么时候落库」。

---

#### 1. 先建立「四层 + 三路审计 + 一读一写」的心智模型

memory 不是「一个组件」，而是**契约 + 实现 + 装配 + 接入**拼起来的一套系统。

**四层结构**：

```
契约层（Protocol + 异常）
    └─ 7 Protocol（Store / Retriever / Manager / Provider / Decay / Forget / Persona）
       + 1 异常层次（MemoryError + 5 子类）
              │
实现层（strategies/default/）
    ├─ store.py      ：MarkdownFileStore（truth source）
    ├─ retriever.py  ：HybridRetriever（BM25 + 强化写回）
    ├─ manager.py    ：DefaultMemoryManager（四操作）
    ├─ decay.py / forget.py / _constants.py
    └─ strategy.py   ：build_default_provider（装配）
              │
装配层（bootstrap.py）
    ├─ provider lifecycle：get / reset / shutdown / set
    ├─ worker lifecycle  ：start / shutdown / get
    └─ 内部辅助          ：_load / _make_journal_callback / _wrap_store
              │
接入层（middlewares/）
    ├─ memory_recall_middleware.py        ：读路径
    └─ memory_consolidation_middleware.py ：写路径
              │
后台（worker.py）
    └─ MemoryWorker：daemon 线程，异步抽取 + 合并
```

**三路审计（traceability）**：

| 路 | 载体 | 记录 |
|---|---|---|
| A | `MemoryTrace.operation_log` | 每次操作，上限 20 条 FIFO（retrieve 不记） |
| B | `journal` 事件 | `memory.*` 事件 |
| C | `OperationLog.actor` | 当前 turn_id（ContextVar 注入） |

**一读一写**：

```
读（同步，每轮）：MemoryMiddleware.abefore_model → retrieve → 注入 HumanMessage
写（异步，每 N 轮）：ConsolidationMW.aafter_model → worker.submit → manager.encode/consolidate
闭环：_wrap_store 把 store 写操作同步到 retriever 索引
```

---

#### 2. 看什么

| 文件 | 看什么 |
|---|---|
| `bootstrap.py` | 双检锁懒加载；`_load_memory_provider` 装配；`_wrap_store` 读写闭环 |
| `config.py` | `MemoryConfig` 字段；`STARTUP_ONLY_FIELDS` 仅 4 个；单例 |
| `memory_recall_middleware.py` | `abefore_model`：提取 query → retrieve → 裁剪 → 注入；`set_turn_id` 注入/清除 |
| `memory_consolidation_middleware.py` | `aafter_model`：数轮次 → 取最近 N*2 条 → worker.submit（非阻塞） |
| `worker.py` | `_run` 消费循环；`_process` 两阶段（抽取 + 合并） |
| `manager.py`（strategies/default/） | 四操作编排；`set_turn_id`；`_compute_trace_id`（F2）；`_add_association_with_lru`（D3） |
| `retriever.py`（strategies/default/） | BM25 + lazy decay + 复合分数；强化写回（1A）；forgotten 过滤（3B）；增量索引（5B） |
| `store.py`（strategies/default/） | 启动加载 + 解析容错；`add` 增量写 vs `update` 全量重写；`batch_update` |
| `decay.py` / `forget.py`（strategies/default/） | Ebbinghaus 三项；两规则遗忘；runtime config 优先 |
| `strategy.py`（strategies/default/） | `build_default_provider` 装配顺序；`shutdown` duck-type |
| `schema.py` / `types.py` | MemoryTrace（frozen）；复合分数公式 |
| 契约层 7 Protocol + `exceptions.py` | 理解实现后对照看，不必先看 |

---

#### 3. 重点看

**3.1. 召回 vs 提取**

| 动作 | 时机 | 同步/异步 |
|---|---|---|
| 召回 | 每次模型调用前（`before_model`） | 同步 |
| 提取 | 每 N 轮触发，worker 里执行 | 异步 |

召回影响 prompt，必须同步；提取耗时长，异步不阻塞。

**3.2. worker 怎么解耦**

`ConsolidationMW.aafter_model → worker.submit(task) → 立即 return None`。
daemon 线程 + 无界 `Queue`。代价：无重试、队列无界（L5 MVP）。

**3.3. 四操作「工具里无 LLM」**

manager 四操作都不调 LLM：

- `encode`：content 由 worker 从 LLM 抽取后传入。
- `consolidate`：merged_content 由 worker 从 LLM 生成后传入。
- `reconsolidate`：new_content 外部传入。

好处：纯存储操作，可 mock、可单测。

**3.4. lazy decay**

strength 不后台定时衰减，而是 retrieve 时按需计算 + 写回（1A）。只有被访问的记忆才花成本。

**3.5. forgotten 标记不删除**

`consolidate` 后旧 trace 标 `metadata.forgotten=True`，不删除（保留回滚）。
过滤在 **Retriever**（3B），store 不感知。

**3.6. `_wrap_store` 闭环**

装配时替换 store 的 add/update/batch_update/remove，每个包装方法「先调原方法 + 再调 retriever.on_trace_*」。
作用：写完立刻能被下一轮检索到。

**3.7. 装配顺序**

```
decay → forget → store → retriever → manager → provider
```
后者依赖前者。

**3.8. `STARTUP_ONLY_FIELDS` 仅 4 个**

`use` / `storage_path` / `vector_store` / `graph_store`。
换需重启（重建 Provider/adapter + derived index）；其余 runtime 可切。

**3.9. 向后兼容**

`config.use == ""` → provider 返 None → 主链短路；`memory_provider is None` → 中间件不挂载。

---

#### 4. 不要看

- Markdown 序列化格式细节、BM25 公式推导、`_constants.py` 具体数值——调优时再看。
- `persona_policy.py`（L6 预留，无实现）。
- `adapters/`（空壳，未接线）。

---

#### 5. 看完这一站应该能回答

1. 四层结构各是什么？谁依赖谁？
2. 召回 / 提取分别在哪个钩子？同步还是异步？
3. `memory_worker` 怎么和主流程解耦？为什么？
4. 「工具里无 LLM」具体指什么？
5. lazy decay 是什么？为什么不跑后台任务？
6. forgotten 为什么不删除？谁过滤？
7. `_wrap_store` 解决什么问题？不装会怎样？
8. `build_default_provider` 的装配顺序？
9. `STARTUP_ONLY_FIELDS` 是哪 4 个？
10. 记忆禁用时主链会怎样？

---

#### 6. 对照

- `_build_middlewares` 里的 `memory_provider is not None → Memory` ↔ `MemoryMiddleware` 挂载条件；
- `_build_middlewares` 里的 `memory_worker is not None → MemoryConsolidation` ↔ `ConsolidationMW` 挂载条件；
- `MemoryMiddleware.abefore_model` 的 `provider.retriever().retrieve(...)` ↔ `HybridRetriever.retrieve`；
- `MemoryMiddleware.abefore_model` 的 `set_turn_id(...)` ↔ `manager.py` 的 `_turn_id_var`；
- `ConsolidationMW.aafter_model` 的 `worker.submit(task)` ↔ `bootstrap.start_memory_worker`；
- `worker._process` 的 `manager.encode / consolidate` ↔ `DefaultMemoryManager` 四操作；
- `bootstrap._load_memory_provider` 的 `_wrap_store` ↔ `HybridRetriever.on_trace_*`；
- `build_default_provider` 的装配顺序 ↔ 「decay → forget → store → retriever → manager → provider」。

### 第 5 站：`skill/`（技能）

**为什么第五**：前四站都聚焦"一次 run 内"的机制（流程、预算、工具、记忆）。skill 不一样——它是一个**跨 run 的、可插拔、可自我改进的能力系统**。看懂它，才知道"技能从哪来、怎么选、怎么注入、怎么打点、怎么评估、怎么进化"。

---

#### 1. 先建立「四层 + 一输入 + 三闭环」的心智模型

skill 不是"一个组件"，而是**四层咬合的完整系统**：

```
输入侧（hub/）
    └─ 发现 / 安装 / 安全 / provenance（技能从外部进来）
              │
L1 基础层（skill/ 根）
    ├─ config.py     ：配置
    ├─ types.py      ：数据契约
    ├─ parser.py     ：SKILL.md → SkillRecord
    ├─ store.py      ：持久化 + version DAG + 4 计数器
    ├─ selector.py   ：读路径决策
    ├─ injector.py   ：渲染注入块
    ├─ _ctx.py       ：跨 hook ContextVar
    └─ __init__.py   ：SkillManager 门面
              │
L3 评估层（skill/eval/）
    └─ 执行 / 任务 / 响应 / 趋势四层评估
              │
L2 进化层（skill/evolution/）
    └─ 触发 / 聚焦 / 变异 / 门控 / 晋升 / 回滚
```

**一输入**：`hub/` 把外部技能（builtin / github / well-known / claude-marketplace）装进 `skills/`，经 `parser` → `store` 落库。

**三闭环**：

```
闭环 A（每轮）：写路径打点 → 4 计数器 → rate 变化 → 下一轮读路径选择
闭环 B（跨层）：eval 产 EvalResult / SkillJudgment / EvolutionSuggestion
                → gate / focuser / trigger 消费
闭环 C（跨轮）：evolution.create_version → 新版本 → eval 下一轮评估
```

**装配总图**：

```
bootstrap
    ├─ hub：4 源 + HubLockFile / SkillsGuard / AuditLog + Installer
    ├─ L1：build_skill_manager() → SkillManager(config) → load_startup(llm)
    ├─ L3：build_default_registry() + EvalLayer → set_eval_layer 回注
    └─ L2：EvolutionManager → set_evolution_manager 回注
```

**关键差异**：hub 管"进来"；L1 管"用"；L3 管"评"；L2 管"改"。四层通过 `SkillManager` 门面 + setter 回注 + 共享数据契约（`types.py`）咬合。

---

#### 2. 看什么

**输入侧（hub/）**

| 文件 | 看什么 |
|---|---|
| `hub/source.py` | `SkillSource` Protocol + `SkillMeta`（源抽象） |
| `hub/sources/builtin_source.py` | 零网络，复用 `SkillManager.search_builtin_skills` |
| `hub/sources/github_source.py` | git clone + identifier 解析（4 种格式）+ 缓存 |
| `hub/sources/claude_marketplace_source.py` / `well_known_source.py` | 拉 registry / 多 endpoint |
| `hub/search.py` | `unified_search`：聚合 + 去重 + 排序 + 截断 + 降级 |
| `hub/installer.py` | 安装六步：fetch → scan → install → hash → lock → audit |
| `hub/hub_store.py` | `HubLockFile`（provenance）+ `SkillsGuard`（安全）+ `AuditLog`（留痕） |

**L1 基础层**

| 文件 | 看什么 |
|---|---|
| `config.py` | `SkillConfig` 字段（开关 / 阈值 / 路径 / evolve_* / eval_config / hub_*） |
| `types.py` | `SkillRecord` / `SkillLineage` / `SkillMetrics` / `SkillHealth` + 4 rate property |
| `parser.py` | SKILL.md frontmatter 解析 + `.skill_id` sidecar + `install` |
| `store.py` | SQLite + WAL + version DAG + 4 计数器 + 迁移链 |
| `selector.py` | `select_for_task`：override + quality filter + LLM / fallback |
| `injector.py` | `build_injection_text`：SkillRecord → markdown 块 |
| `_ctx.py` | `_active_skills_ctx` / `_applied_ctx` 跨 hook 桥 |
| `__init__.py` | `SkillManager` 门面 + `build_skill_manager` + `load_startup` |

**L3 评估层（eval/）**

| 文件 | 看什么 |
|---|---|
| `eval/types.py` | `SkillJudgment` / `TaskQualityScore` / `ContractRule` / `SkillHealthReport` / `EvalRun` / `EvolutionSuggestion` |
| `eval/protocols.py` | 5 个 Protocol（analyzer / judge / checker / tracker / store） |
| `eval/analyzers/checks.py` | 确定性检查函数库（单一真相源） |
| `eval/analyzers/contract_compiler.py` | 按 skill 文本编译规则 |
| `eval/analyzers/response_contract_checker.py` | 响应层执行 + 打分 |
| `eval/analyzers/skill_judgment_analyzer.py` | 执行层 LLM 判断 + 进化建议 |
| `eval/analyzers/task_quality_judge.py` | 任务层 4 维加权 |
| `eval/registry.py` | `EvalRegistry` + `RegistryEvalBridge`（L2 唯一可见的 L3 实现） |

**L2 进化层（evolution/）**

| 文件 | 看什么 |
|---|---|
| `evolution/types.py` | `EvolutionContext` / `EvolutionRecord` / `EvalResult` / `GateDecision` / 6 枚举 |
| `evolution/protocols.py` | 5 个 Protocol（trigger / focuser / mutator / eval_bridge / gate） |
| `evolution/manager.py` | `EvolutionManager`：闭环七步编排 |
| `evolution/triggers/metric_monitor.py` | 自动 FIX：两阶段筛选 + anti-loop |
| `evolution/triggers/capture_trigger.py` | 手动 CAPTURED（2a 无自动信号） |
| `evolution/focus/ive_focuser.py` | IVE 5 问诊断 + implementation 累计升级 |
| `evolution/mutators/llm_mutator.py` | FIX 编辑 / CAPTURED 生成 + budget 截断 |
| `evolution/gates/score_delta_gate.py` | 晋升决策门（零 LLM） |
| `evolution/gates/git_ratchet.py` | 上线后退化回滚 |
| `evolution/eval/programmatic_bridge.py` | L2 兼容评估桥（L3 关闭时） |

---

#### 3. 重点看

##### 3.1. 四层各自的角色与边界

| 层 | 做什么 | 不做什么 |
|----|--------|---------|
| hub | 发现 / 安装 / 安全 / provenance | 不参与运行期选择 / 注入 / 打点 |
| L1 | 选择 / 注入 / 打点 / 持久化 | 不评估 / 不进化 |
| L3 | 评估 / 评分 / 产建议 | 不决策晋升 / 不执行进化 |
| L2 | 触发 / 聚焦 / 变异 / 门控 / 晋升 / 回滚 | 不做评估逻辑 / 不直接写 L1（除 rollback / create_version） |

##### 3.2. 读路径与写路径

**读路径（每轮，`before_model`）**：

```
SkillInjectionMiddleware.before_model
    ├─ _selector.select_for_task(task, overrides)     = _selector
    │     ├─ override 强制包含
    │     ├─ quality filter（anti-loop）
    │     ├─ 候选 ≤ max → 全返
    │     ├─ 候选 > max + llm → LLM 选
    │     └─ fallback → effective_rate 降序
    ├─ build_injection_text(selected)                 = injector
    └─ 写 _active_skills_ctx（供写路径读）
```

**写路径（每轮，`awrap_tool_call` + `after_agent`）**：

```
SkillMetricsMiddleware
    ├─ awrap_tool_call：读 _active_skills_ctx
    │     └─ tool_name ∈ allowed_tools → 写 _applied_ctx
    └─ after_agent：读 _applied_ctx
          └─ store.record_outcome（累加 4 计数器）
```

**关键**：`_ctx.py` 是唯一桥——因为 `awrap_tool_call` 没有 `state` 参数。

##### 3.3. 三处 anti-loop

| 位置 | 机制 |
|------|------|
| Trigger 层 | `min_selections` + `cooldown_turns`（数据驱动，`mark_evolved` 更新锚点） |
| Focuser 层 | implementation 累计 ≥ `impl_fail_threshold` → 升级 FUNDAMENTAL |
| Ratchet 层 | `selections < min` 不评判回滚 |

##### 3.4. 装配与回注

- **L1**：`build_skill_manager` 构造 → `load_startup(llm)` 装配 selector / 中间件。
- **L3 / L2**：bootstrap 构造 → `set_eval_layer` / `set_evolution_manager` **回注**。
- **为什么回注**：L3 / L2 反向依赖 L1（store）与彼此类型，直接在建会成环。

##### 3.5. 与中间件的耦合点

| 中间件 | 与 skill 的耦合 |
|--------|---------------|
| `SkillInjectionMiddleware` | 调 `_selector` + `injector`；写 `_active_skills_ctx` |
| `SkillMetricsMiddleware` | 读 `_active_skills_ctx`；写 `_applied_ctx`；调 `store.record_outcome` |
| `SkillActivationMiddleware` | 技能激活（`/skill use`） |
| `SkillInjectionMiddleware` / `HelpRequestMiddleware` | 共用 `agent_tools/skill_search` |

##### 3.6. 关键设计原则

| 原则 | 说明 |
|------|------|
| 零侵入 | L2 / L3 通过 Protocol 与 L1 解耦；替换实现不改核心 |
| 内容/索引分离 | DB 只存 `path + content_hash`；SKILL.md 全文留文件 |
| 单指针 + DAG | `is_active` 单指针；`create_version` / `rollback` 只切指针不删行 |
| 4 计数器零 LLM | `record_selection` / `record_outcome` 纯 SQL 累加 |
| 降级分级 | 响应层 fail-closed；执行 / 任务层 fail-silent；搜索层 silent |
| opt-in | `enabled=false` → `build_skill_manager` 返 None，主链短路 |

##### 3.7. 数据落点

| 层 | 写入 | 目标 |
|----|------|------|
| hub | `parser.install` | `skills/` |
| hub | `HubLockFile.add` / `AuditLog.append` | `~/.poirot/skills/.hub/` |
| L1 | `register` / `record_selection` / `record_outcome` / `create_version` | `skill_records` |
| L3 | `save_judgment` / `save_task_score` / `save_eval_run` | `skill_eval_judgments` / `task_quality_scores` / `skill_eval_runs` |
| L2 | `record_evolution` | `skill_evolutions` |

---

#### 4. 不要看

- **SQLite schema 细节、BM25 公式、skill_id 生成规则**——先建立"四层 + 三闭环"心智模型，细节留调优。
- **每个 analyzer 的 LLM prompt 文案**——关注"输入 / 输出 / 降级"，不关注 prompt 微调。
- **hub 各源的 MVP 占位实现**（`GitHubSource.search` / `ClaudeMarketplaceSource.fetch` 等）——知道它们是占位即可。
- **2b / L3 预留的 Protocol**（ChampionGate / HITLGate / CompositeGate / ValidationGate / MultiJudgeGate）——知道存在、知道是替换点即可。

---

#### 5. 看完这一站应该能回答

1. skill 的四层各是什么？谁依赖谁？为什么 L2 / L3 要回注？
2. hub 的四个源各有什么能力？`sources` 字典的 key 用来做什么？
3. 安装六步是什么？`SkillsGuard` 在哪一步拦截？
4. `SkillRecord` 和 `SkillMeta` 有什么区别？
5. `SkillSelector.select_for_task` 的候选裁剪有几条分支？
6. 为什么需要 `_ctx.py`？没有它会怎样？
7. 4 计数器分别在哪一步被累加？`record_outcome` 的四条规则是什么？
8. eval 的四层分别在哪触发？各自产什么？
9. evolution 的闭环七步是什么？三处 anti-loop 在哪？
10. 三条闭环（A / B / C）分别是什么？
11. 为什么 `record_outcome` 是"唯一反向写 L1 计数器"的 eval 组件？
12. 2a 的能力边界是什么？哪些留 2b / L3？

---

#### 6. 对照

- `_build_middlewares` 里的 `skill_manager is not None → SkillInjection + SkillMetrics + SkillActivation` ↔ `SkillManager.load_startup` 装配的中间件。
- `skill_injection_middleware.py` 的 `select_for_task` ↔ `selector.py` 的 `SkillSelector`。
- `skill_metrics_middleware.py` 的 `_active_skills_ctx` / `_applied_ctx` ↔ `_ctx.py`。
- `skill_metrics_middleware.py` 的 `store.record_outcome` ↔ `store.py` 的 4 计数器。
- `SkillManager.set_eval_layer` / `set_evolution_manager` ↔ bootstrap 的 L3 / L2 装配。
- `eval/registry.py` 的 `RegistryEvalBridge.evaluate` ↔ `evolution/manager.py` 的 `_run_evolution` 第 3 步。
- `evolution/manager.py` 的 `create_version` ↔ `store.py` 的 version DAG。
- `evolution/gates/score_delta_gate.py` 的 `decide` ↔ `EvolutionContext` 的 `eval_result`。

---

**一句话总览**：

```
hub（进来）→ L1（用：选择 / 注入 / 打点）→ L3（评）→ L2（改）→ 回到 L1
   ①            ②③④⑤              ⑥         ⑦
```

**skill 的本质**：一个自我改进的技能系统——hub 把外部技能装进来，L1 在每轮对话中选择并注入、同时打点，L3 事后评估质量，L2 据此触发进化、产生新版本，新版本再回到 L1 被使用与评估。四层通过 `SkillManager` 门面 + setter 回注 + 共享数据契约咬合成闭环。

---

### 第 6 站：`sandbox/`（沙箱）

**为什么第六**：前五站都聚焦"能力"（流程、上下文、工具、记忆、技能）——它们决定 Agent **能做什么**。sandbox 不一样，它决定 Agent **能在哪里做、做得多安全**。看懂它，才知道 `bash` / `read_file` 这些工具真正在哪执行、危险操作被谁拦、本地和 Docker 到底差在哪。

---

#### 1. 先建立「一门面 + 三组件 + 两套实现」的心智模型

sandbox 不是"一个组件"，而是**契约 + 实现 + 装配 + 接入**拼起来的一套系统。

**三层结构**：

```
契约层（contracts/ + types.py + exceptions.py）
    └─ 5 契约（Provider / Backend / Runtime / Guard / Translator）
       + 公共类型（PathMapping / SandboxInfo / GrepMatch）
       + 异常树（SandboxError 为根）
              │
实现层（local/ + docker/ + runtimes/ + translators/ + guards/）
    ├─ local/      ：LocalSandboxProvider（组合本地三件套）
    ├─ docker/     ：DockerSandboxProvider（生命周期编排）
    │     ├─ LocalContainerBackend（容器 CRUD）
    │     ├─ DockerExecutor（CLI 执行落点抽象）
    │     ├─ readiness（就绪轮询）
    │     └─ cross_process_lock（跨进程锁）
    ├─ runtimes/   ：LocalRuntime（subprocess）/ DockerRuntime（HTTP）
    ├─ translators/：Identity / Local / Docker 三个实现
    └─ guards/     ：LocalSecurity / DockerPath / Permissive / Audit
              │
装配与接入层（integration/）
    ├─ config.py             ：SandboxConfig（startup-only）
    ├─ context.py            ：ContextVar 传 sandbox_id
    ├─ tools.py              ：make_sandbox_tools（工具工厂）
    └─ bootstrap_sandbox.py  ：register_sandbox_shutdown（atexit 收尾）
              │
接入 Agent（middlewares/sandbox_middleware.py）
    └─ before_agent 启动沙箱、写 state；wrap_tool_call 路由工具调用
```

**一门面**：`Sandbox` 具体类（非 ABC），组合 **Runtime + Translator + Guard** 三可替换组件，编排四步：

```
guard.validate → translator.translate → runtime.execute → translator.mask
```

**两套实现**：

| | Local | Docker |
|---|---|---|
| Provider | `LocalSandboxProvider` | `DockerSandboxProvider` |
| Runtime | `LocalRuntime`（宿主 subprocess） | `DockerRuntime`（HTTP 调容器） |
| Translator | `LocalPathTranslator`（双向翻译） | `DockerPathTranslator`（正向直传 + 反向映射） |
| Guard | `AuditGuard(LocalSecurityGuard)` | `AuditGuard(DockerPathGuard)` |
| Backend | **无** | `LocalContainerBackend` |
| 隔离级别 | 软约束 | 硬隔离 |

**核心规律**：Docker 把「隔离」和「路径对齐」下沉到内核 / 挂载层，所以 guard / translator 反而更简单；但新增了「容器生命周期」和「远程执行」两整套机制。

---

#### 2. 看什么

**阶段 ①：公共基础（先建词汇表）**

| 文件 | 看什么 |
|---|---|
| `types.py` | `PathMapping`（frozen）/ `ResolvedPath` / `GrepMatch` / `SandboxInfo`（跨进程恢复） |
| `exceptions.py` | `SandboxError` 为根；工具层只需 catch 根；guard 拦截抛 `SandboxPermissionError` |
| `contracts/*.py` | 5 契约：Provider / Backend / Runtime / Guard / Translator 的方法语义 |
| `integration/config.py` | `SandboxConfig` 结构；`use` 为空不启用；`allow_host_bash` 决定是否注册 bash |
| `integration/context.py` | ContextVar 传 `sandbox_id`（并发隔离） |

**阶段 ②：门面 + 编排**

| 文件 | 看什么 |
|---|---|
| `sandbox.py` | 门面持有 runtime / translator / guard；编排四步顺序；`execute_command` 怎么调三组件 |

**阶段 ③：本地实现（最简单，先跑通）**

| 文件 | 看什么 |
|---|---|
| `local/local_sandbox_provider.py` | LRU 缓存 + 确定性 ID；组合本地三件套 |
| `runtimes/local_runtime.py` | `subprocess` 裸执行；异常统一为 `SandboxError` 子类 |
| `translators/local_path_translator.py` | 双向翻译 + 防穿越 + 缓存；`mask_output` 是逆操作 |
| `translators/identity_translator.py` | 恒等直传（三个方法都不转换） |
| `guards/local_security_guard.py` | 命令黑名单 + 路径白名单 + shlex fail-closed；**唯一防线** |
| `guards/permissive_guard.py` | 全放行；用于容器已隔离的场景 |
| `guards/audit_guard.py` | 组合层：三档分级（block/warn/pass）+ 审计 |
| `utils/file_operation_lock.py` | 进程内锁；**S11 限制：不跨进程** |
| `utils/sandbox_id.py` | sandbox_id 格式校验（防路径穿越） |

**阶段 ④：Docker 实现（真正的隔离）**

| 文件 | 看什么 |
|---|---|
| `docker/docker_sandbox_provider.py` | 三层缓存 + warm_pool + idle_checker；release 移入 warm_pool |
| `docker/local_container_backend.py` | 容器 CRUD；**executor 在其之下** |
| `docker/executor.py` | `DockerExecutor` 抽象；Local / WSL 两实现 |
| `docker/readiness.py` | 就绪轮询（sync + async） |
| `docker/cross_process_lock.py` | 跨进程文件锁；与阶段 ③ 的 `file_operation_lock` 对比 |
| `runtimes/docker_runtime.py` | **经 HTTP 调容器内 AIO**；不在容器里，是"遥控器" |
| `guards/docker_path_guard.py` | 写入必须落挂载区；读不限制 |
| `translators/docker_path_translator.py` | 正向直传 + 反向 `reverse_translate` |
| `docker/remote_container_backend.py` | K8s 模式（仅 sandbox_url） |

**阶段 ⑤：装配与接入**

| 文件 | 看什么 |
|---|---|
| `integration/tools.py` | `make_sandbox_tools` 产出 6 个工具（`allow_host_bash=False` 时 5 个） |
| `integration/bootstrap_sandbox.py` | `register_sandbox_shutdown` 挂 atexit；**只管收尾** |
| `middlewares/sandbox_middleware.py` | `before_agent` 启动 + 写 state；`wrap_tool_call` 路由 |
| `multiagent/sandbox_binder.py` | 多 Agent 场景下沙箱绑定 / 共享 |

---

#### 3. 重点看

**3.1. 一次 bash 调用的完整链路**

```
① 工具层 integration/tools.py
bash_tool(command)
    └─ _ensure_sandbox(provider)
          ├─ get_sandbox_id()          ← ContextVar
          └─ provider.get(sandbox_id)

② Sandbox 门面 sandbox.py
sandbox.execute_command(cmd)
    ├─ ① guard.validate_command(cmd)        ← 命令级拦截
    ├─ ② guard.validate_path(path)          ← 路径级拦截
    ├─ ③ translator.translate_command(cmd)  ← 虚拟 → 真实路径
    ├─ ④ runtime.exec_command(cmd)          ← 裸执行
    └─ ⑤ translator.mask_output(output)     ← 真实 → 虚拟（脱敏）

③ Docker 专属分层（仅在 runtime 之下）
    ├─ cross_process_lock.acquire()
    ├─ readiness.wait_until_ready()
    ├─ executor.exec(container_id, cmd)     ← docker exec
    └─ cross_process_lock.release()
```

**关键**：公共流程到 `runtime` 分叉；本地直接 `subprocess`，Docker 走 HTTP 调容器内 AIO。

**3.2. 三组件的职责边界**

| 组件 | 管什么 | 不管什么 |
|---|---|---|
| Runtime | 裸执行（跑命令 / 读写文件） | 路径翻译、安全检查 |
| Translator | 虚拟 ↔ 真实路径翻译 + 输出脱敏 | 执行、校验 |
| Guard | 安全校验（validate only） | 脱敏（归 translator） |

**3.3. Docker 的两条独立链路**

```
链路 A：容器生命周期
    DockerSandboxProvider
        └─ LocalContainerBackend
              ├─ DockerExecutor → subprocess.run("docker run/inspect/stop")
              ├─ readiness（等就绪）
              └─ cross_process_lock（临界区保护）

链路 B：容器使用
    Sandbox（provider._make_sandbox 构造）
        ├─ DockerRuntime → HTTP → 容器内 AIO
        ├─ DockerPathTranslator
        └─ AuditGuard(DockerPathGuard)

两条链路通过 SandboxInfo.sandbox_url 间接握手
```

**3.4. `DockerExecutor` 与 `DockerRuntime` 的关系**

- **无直接关系**——分属两条链路：
  - `DockerExecutor` 管"docker CLI 在哪跑"（链路 A 底层）。
  - `DockerRuntime` 管"怎么调容器内服务"（链路 B 本身）。
- **`DockerRuntime` 在宿主进程，不在容器里**——它是"遥控器"，发 HTTP 请求。
- **设计精髓**：`DockerRuntime` 与 `LocalRuntime` 实现同一套契约——换 runtime 等于换执行环境。

**3.5. 本地 vs Docker 的复杂度反直觉**

| 维度 | Local | Docker |
|---|---|---|
| Guard 角色 | 唯一防线 → 复杂 | 补漏层 → 简单 |
| 路径翻译 | 双向都复杂 | 正向直传 + 反向一处 |
| 命令执行 | `subprocess.run` 在宿主 | HTTP 调容器内 AIO |
| 基础设施层 | **无** | `LocalContainerBackend` |
| 生命周期 | runtime 几乎 no-op | provider 管三层缓存 + warm_pool + idle |
| 信号处理 | 仅 atexit | atexit + SIGTERM/SIGINT/SIGHUP |

**核心规律**：Docker 把「隔离」和「路径对齐」下沉到内核 / 挂载层，所以 guard / translator 变简单；同时新增「容器生命周期」和「远程执行」两套机制，所以 provider / runtime / backend 变复杂。

**3.6. Guard 的两条正交分类轴**

| 轴 | 实现 | 回答 |
|---|---|---|
| 隔离类型 | `LocalSecurityGuard` / `DockerPathGuard` / `PermissiveGuard` | "这个隔离实现下，什么该拦？" |
| 职责类型 | `AuditGuard`（组合层） | "要不要叠加分级 + 审计？" |

实际组合：`AuditGuard(LocalSecurityGuard)` 或 `AuditGuard(DockerPathGuard)`。

**3.7. Translator 的三种实现**

| 实现 | 适用 | 特点 |
|---|---|---|
| `IdentityTranslator` | Docker（对齐）/ E2B | 三个方法都直传 |
| `LocalPathTranslator` | 本地（虚拟 ≠ 物理） | 双向翻译 + 防穿越 + 缓存 |
| `DockerPathTranslator` | Docker（需 artifact 提取） | 正向直传 + 反向 `reverse_translate` |

**3.8. 生命周期**

```
创建 → provider.acquire(thread_id, user_id)
    ├─ 本地：组合三件套
    └─ Docker：三层缓存 / warm_pool / discover+create

使用 → sandbox.execute_command(cmd)
    └─ 四步编排（validate → translate → execute → mask）

释放 → provider.release()
    ├─ 本地：no-op（保留缓存）
    └─ Docker：移入 warm_pool（容器继续运行）

销毁 → provider.shutdown() / _destroy_active()
    └─ 清状态 + close + destroy

兜底 → bootstrap_sandbox 的 atexit（进程退出统一 shutdown）
```

**3.9. 三处安全 / 正确性修复**

| 编号 | 内容 |
|---|---|
| S2 | `allow_host_bash=False` 禁用 bash 工具 |
| S5 | `acquire_async` 用 `acquired` flag 防 cancel 误 release |
| S7 | `destroy` 前二次 discover 校验，避免误杀同 ID 新容器 |
| S9 | `list_dir` BFS 剪枝，防大目录 DoS |
| S10 | `grep` ReDoS 防护（拒超长 pattern + 嵌套量词） |
| S11 | `file_operation_lock` 的进程内限制声明 |
| #1433 | `DockerRuntime._lock` 串行化，防并发破坏持久 shell session |
| #2872 | `DockerRuntime.close` 属性链挖掘 httpx.Client.close |

**3.10. 工具数量与条件挂载**

- `make_sandbox_tools` 产出 **6 个工具**：`bash` / `read_file` / `write_file` / `list_dir` / `str_replace` / `present_files`。
- `allow_host_bash=False` 时**不注册 `bash`** → 5 个工具（S2 安全加固）。
- `sandbox_provider is not None` → 挂 `SandboxMiddleware`；否则不挂。

---

#### 4. 不要看

- **Docker CLI 参数细节**（`--security-opt` / `--mount` 的具体拼法）——先建立"两条链路 + 三组件"心智模型，细节留到调试时看。
- **时间戳解析、端口重试、JSON 解析容错**——这些是健壮性细节，知道有即可。
- **`remote_container_backend.py` 的 K8s 细节**——当前只用 local 版，remote 是预留。
- **`_scan_bfs` / `_validate_regex_pattern` 的具体算法**——知道它们防什么（DoS / ReDoS）即可。
- **`file_operation_lock` 的 WeakValueDictionary 细节**——知道它是进程内锁、不跨进程即可。

---

#### 5. 看完这一站应该能回答

1. `bash` 工具调用时，命令到底在哪执行？本地和 Docker 分别走什么路径？
2. 虚拟路径（如 `/mnt/poirot/user-data/a.txt`）映射到宿主机哪里？
3. 危险命令怎么被拦截？规则是什么？guard 抛异常还是返回 bool？
4. 沙箱什么时候启动、什么时候销毁？`bootstrap_sandbox` 管什么？
5. 多个 Agent 共享同一个沙箱吗？锁的粒度是什么？
6. `Sandbox` 门面持有哪些对象？编排顺序是什么？
7. Guard 的两条正交分类轴是什么？`AuditGuard` 和 `LocalSecurityGuard` 是什么关系？
8. Translator 的三个实现分别适用于什么场景？`mask_output` 是逆操作吗？
9. `DockerExecutor` 和 `DockerRuntime` 是什么关系？
10. `DockerRuntime` 在容器里执行吗？它是"遥控器"还是"执行者"？
11. `make_sandbox_tools` 产出几个工具？`allow_host_bash=False` 时呢？
12. 为什么 Docker 的 guard / translator 反而比本地简单？

---

#### 6. 对照

- `_build_middlewares` 里的 `sandbox_provider is not None → Sandbox` ↔ `SandboxMiddleware` 挂载条件。
- `sandbox_middleware.py` 的 `before_agent` 启动沙箱 ↔ `provider.acquire(thread_id, user_id)`。
- `sandbox_middleware.py` 的 `wrap_tool_call` 路由 ↔ `provider.get(sandbox_id)` + `sandbox.execute_command`。
- `integration/tools.py` 的 `_ensure_sandbox` ↔ `integration/context.py` 的 `get_sandbox_id`。
- `integration/tools.py` 的 `_truncate_output` ↔ `sandbox.execute_command` 返回后的输出处理。
- `guards/audit_guard.py` 的 `_classify` ↔ block/warn/pass 三档分级；`LocalSecurityGuard` 被包在 inner。
- `translators/local_path_translator.py` 的 `translate_path` / `mask_output` ↔ Sandbox 编排的 ③ 和 ⑤ 步。
- `runtimes/docker_runtime.py` 的 `exec_command` ↔ HTTP 调容器内 AIO；不走 `LocalContainerBackend`。
- `docker/docker_sandbox_provider.py` 的 `_make_sandbox` ↔ 组合 DockerRuntime + DockerPathTranslator + AuditGuard(DockerPathGuard)。
- `docker/local_container_backend.py` 的 `create` / `destroy` ↔ 容器生命周期；由 provider 调，不由 runtime 调。

---

**总览**：

```
契约（5 个）→ 门面（Sandbox 编排四步）→ 两套实现（Local / Docker）→ 装配接入（integration）→ 挂到 Agent（middleware）
   ①              ②                    ③④                    ⑤
```

**sandbox 的本质**：一个可替换的执行边界——`Sandbox` 门面组合 **Runtime（裸执行）+ Translator（路径翻译 + 脱敏）+ Guard（安全校验）** 三可替换组件，编排 `validate → translate → execute → mask` 四步。本地实现三件套都复杂（无隔离，Guard 是唯一防线）；Docker 实现把隔离和路径对齐下沉到内核/挂载层，guard / translator 变简单，但新增了容器生命周期和远程执行两套机制。理解 Docker 的关键是分清两条链路——容器生命周期走 backend/executor 调 docker CLI，容器使用走 runtime 调 HTTP，两者互不调用，只通过 `SandboxInfo.sandbox_url` 间接握手。

### 第 7 站：`multiagent/`（多 Agent）

**为什么最后**：前六站都聚焦"单体 Agent 的能力"（流程、上下文、工具、记忆、技能、沙箱）——它们决定 Agent **能做什么、在哪做、多安全**。multiagent 不一样，它决定 Agent **能不能把活派给别人做**。看懂它，才知道 Leader 怎么选 specialist、外部进程怎么被启动、结果怎么回到 Leader，以及 L1/L2/L3 三层怎么形成"记录 → 评估 → 进化"的闭环。

---

#### 1. 先建立「三层 + 四 specialist + 七契约 + 两座桥」的心智模型

multiagent 不是"一个组件"，而是**契约 + 实现 + 装配 + 三层学习体系**拼起来的一套系统。

**三层结构**：

```
契约层（7 个 Protocol + types + exceptions）
    └─ SpecialistAgent / SpecialistRuntime / SubagentProvider /
       CredentialProvider / ContextSummarizer / ResultSummarizer / SandboxBinder
       + 公共类型（SpecialistRequest / Result / RawResult / Capability）
       + 异常树（SpecialistError + SubagentError 两条独立层次）
              │
实现层（specialists/ + runtimes/ + credentials/ + summarizers/ + installer/ + extensions/）
    ├─ specialists/ ：4 个 specialist（subagent / claude / codex / pi）
    ├─ runtimes/    ：4 个 runtime（进程内 / CLI / ACP / RPC）
    ├─ credentials/ ：3 个凭证提供者（claude / codex / pi）
    ├─ summarizers/ ：4 个 context + 5 个 result（含 base）
    ├─ installer/   ：PiInstaller（后台 npm install）
    └─ extensions/  ：pi-sandbox-bridge/index.ts（TS 跨语言桥）
              │
装配与接入层（bootstrap.py + registry.py + middleware.py + tools.py + metrics.py）
    ├─ bootstrap.py   ：setup_multiagent（装配入口）
    ├─ registry.py    ：SpecialistRegistry（花名册）
    ├─ middleware.py  ：OrchestrationMiddleware（拦截 delegate_to_*）
    ├─ tools.py       ：make_specialist_tool（工具工厂）
    └─ metrics.py     ：L1 指标存储（SQLite，9 张表）
              │
三层学习体系
    ├─ L1 运行层 ：metrics.py（常开，打点）
    ├─ L2 进化层 ：evolution/（默认关，变异模板）
    └─ L3 评估层 ：eval/（默认关，打分）
              │
接入 Agent（leader/factory.py + leader/agent.py）
    └─ Leader factory 注入 specialist_tools + orchestration_middleware
        └─ LLM 看到 delegate_to_* 工具
```

**四 specialist**：

| | subagent | claude | codex | pi |
|---|---|---|---|---|
| 本质 | 进程内自复制 | 外部 CLI | 外部 ACP | 外部 CLI + RPC + TS 扩展 |
| capability | RESEARCH | REVIEW | CODING | CODING |
| 执行 | `agent.invoke` | `subprocess.run` | ACP 异步 | `subprocess.Popen` + RPC |
| 凭证 | 继承 Leader | `~/.claude/.credentials.json` | `~/.codex/auth.json` | 13 个 env + config |
| 装配闸门 | 无 | 凭证探测 | 凭证探测 | **安装器 + 凭证** |

**七契约**（全部 Protocol，实现方无需继承）：

| 契约 | 文件 | 规定 |
|---|---|---|
| `SpecialistAgent` | `specialist.py` | name / capabilities / invoke |
| `SpecialistRuntime` | `specialist_runtime.py` | invoke（裸执行，sync only） |
| `SubagentProvider` | `subagent.py` | spawn（自复制） |
| `CredentialProvider` | `credential_provider.py` | get_credential（只发现不管理） |
| `ContextSummarizer` | `context_summarizer.py` | summarize（输入端） |
| `ResultSummarizer` | `result_summarizer.py` | summarize（输出端） |
| `SandboxBinder` | `sandbox_binder.py` | bind（multiagent ↔ sandbox 桥） |

**两座桥**：

- `sandbox_binder.py`：multiagent 与 sandbox 的桥（沙箱绑定）。
- `mcp/specialist_mcp_server.py`：specialist 反向调 Poirot 8 沙箱接口的桥。

**核心规律**：**Specialist 持有 Runtime（组合，非继承）**——Specialist 是"Agent 语义封装"，Runtime 是"进程/协议执行器"。**两个摘要器独立**（进 / 出两个契约）。**subagent 与异构 specialist 是两条并列路径**（不是子集关系）。

---

#### 2. 看什么

**阶段 ①：公共基础（先建词汇表）**

| 文件 | 看什么 |
|---|---|
| `config.py` | `MultiAgentConfig` 结构；`enabled` 默认 **True**；`STARTUP_ONLY_FIELDS`；`L2Config` / `L3Config` / `BudgetConfig` 嵌套 |
| `types.py` | 核心数据契约：`SpecialistRequest` / `SpecialistRawResult` / `SpecialistResult` / `SubagentRequest` / `SubagentResult` / `SpecialistCapability`；**全部 frozen** |
| `exceptions.py` | **两条独立层次**：`SpecialistError`（外部异构）+ `SubagentError`（内部自复制）；`details` 展开格式统一 |
| `metrics.py` | L1 指标存储（**9 张表**）；4 计数器 + 明细；同时是 L2/L3 的持久化层 |
| `contracts 层`（7 个文件） | `specialist.py` / `specialist_runtime.py` / `subagent.py` / `credential_provider.py` / `context_summarizer.py` / `result_summarizer.py` / `sandbox_binder.py` |

**阶段 ②：装配与运行时核心**

| 文件 | 看什么 |
|---|---|
| `bootstrap.py` | `setup_multiagent` 装配主入口；enabled=false → `_EMPTY_SETUP`；**L3 嵌套在 L2 里** |
| `registry.py` | `SpecialistRegistry`：名字注册；`get` 缺失抛 `SpecialistNotFoundError`；**实例级非全局单例** |
| `middleware.py` | `OrchestrationMiddleware`：拦截 `delegate_to_*`；**打点 + 汇总 + fail-closed** |
| `tools.py` | `make_specialist_tool` / `make_subagent_tool`；**动态生成工具**；ContextVar 传 ThreadState |

**阶段 ③：自复制路径（最简单，先跑通）**

| 文件 | 看什么 |
|---|---|
| `specialists/subagent_specialist.py` | 纯适配器：`invoke` 只转发给 runtime；`name="subagent"`，`capability=RESEARCH` |
| `runtimes/subagent_runtime.py` | `_get_agent` → `agent_factory()`；`_create_isolated_state`（全新 ThreadState + 复用父 sandbox_id）；`_extract_output` |
| `summarizers/context/self_copy_context_summarizer.py` | 规则路径（无 LLM）：goal + user_input + observations[-5:]；截断 3000 字符 |
| `summarizers/result/base.py` | **模板方法**：`summarize` 固定流程 + 钩子（`_evaluate_success` / `_extract_gap` / `_compress` / `_classify_failure`） |
| `summarizers/result/self_copy_result_summarizer.py` | **纯继承** BaseResultSummarizer，零覆写 |

**阶段 ④：外部异构 Specialist（真正的 multiagent）**

*ClaudeCode 路径：*

| 文件 | 看什么 |
|---|---|
| `runtimes/claude_code_runtime.py` | `subprocess.run(["claude", "--print", goal])`；透传 4 个 auth env；异常映射 |
| `credentials/claude_credential.py` | 三级查找（env token > env 路径 > 默认文件）；过期检测 |
| `specialists/claude_code_specialist.py` | name="claude"，capability=REVIEW |
| `summarizers/context/claude_code_context_summarizer.py` | 提代码块（最多 2）+ review 关键词（最多 5）；零 LLM |
| `summarizers/result/claude_code_result_summarizer.py` | 覆写 `_evaluate_success`（+suggestion）+ `_extract_gap` |

*Codex 路径：*

| 文件 | 看什么 |
|---|---|
| `runtimes/codex_runtime.py` | ACP 协议 + stdio JSON-RPC；同步壳包异步；lazy import `acp` 包 |
| `credentials/codex_credential.py` | 兼容 legacy + nested 两种 JSON 格式 |
| `specialists/codex_specialist.py` | name="codex"，capability=CODING |
| `summarizers/context/codex_context_summarizer.py` + `summarizers/result/codex_result_summarizer.py` | 提代码 + 文件路径；覆写（+0 passed 检查） |

*Pi 路径（最复杂）：*

| 文件 | 看什么 |
|---|---|
| `installer/pi_installer.py` | 后台 npm install（daemon 线程）；flag 文件；**为什么 Pi 需要而 Claude/Codex 不需要** |
| `credentials/pi_credential.py` | 双轨解析：config 优先 + env 兜底；13 个 provider（国内优先） |
| `runtimes/pi_runtime.py` | `pi --mode rpc` + stdio JSON-RPC；**强制走 MCP**（`--no-builtin-tools` + `-e extension`）；**唯一返回 token 用量** |
| `specialists/pi_specialist.py` | name="pi"，capability=CODING；**唯一接收 credential 参数** |
| `summarizers/context/pi_context_summarizer.py` | 提代码（finditer）+ 文件路径（正则扫 observations + messages） |
| `summarizers/result/pi_result_summarizer.py` | **覆写 summarize**（三段解析：What You Did / Success / Gaps） |
| `extensions/pi-sandbox-bridge/index.ts` | **TS extension**：注册 8 个 `poirot_*` 工具；jiti 加载 |
| `mcp/specialist_mcp_server.py` | specialist 通过 MCP 调 Poirot 8 接口 |

**阶段 ⑤：接入 Leader**

| 文件 | 看什么 |
|---|---|
| `agents/leader/agent.py` | Leader 在哪一步决定派活；**soft routing**（LLM 自决） |
| `agents/leader/factory.py` | Leader 怎么拿到 `specialist_tools` + `orchestration_middleware` |
| `agents/leader/prompts.py` + `prompts/system/leader/*.md` | 提示词怎么描述 specialist |
| `multiagent/tools.py`（回看） | **工具注入点**：`make_specialist_tool` 产出的工具进 Leader tools |
| `multiagent/middleware.py`（回看） | `OrchestrationMiddleware.wrap_tool_call` 拦截 |
| `multiagent/sandbox_binder.py` | 多 Agent 场景下沙箱怎么绑 |
| `agents/middlewares/skill_activation_middleware.py` | 技能激活和 specialist 选择的关系 |
| `app/services/stream_service.py` | 子 Agent 过程怎么流式回前端 |

**阶段 ⑥：L2 / L3 学习体系**

*L2 evolution：*

| 文件 | 看什么 |
|---|---|
| `evolution/types.py` | `EvolutionArtifact` / `FailureCategory` / `ContextSummaryTemplate`（W2）/ `SkillInjectionTemplate`（W4） |
| `evolution/bootstrap.py` | `setup_l2` 装配入口 |
| `evolution/trigger_manager.py` | 四源触发 + 1h 冷却 |
| `evolution/failure_focuser.py` | 失败聚焦（不调 LLM） |
| `evolution/evolution_mutator.py` | LLM 变异执行 |
| `evolution/promotion_gate.py` | 评估 + 晋升（★ **L3 调用点**） |
| `evolution/version_dag.py` | 版本持久化 + is_active 单指针 |
| `evolution/worker.py` | daemon 线程消费 |
| `evolution/metrics_l2.py` | 11 种事件打点 |
| `evolution/metrics_view.py` | 读 L1 指标契约 |
| `evolution/budget_guard.py` | 三维度预算 |
| `evolution/trigger_middleware.py` | L1 graph 钩子 |

*L3 eval：*

| 文件 | 看什么 |
|---|---|
| `eval/types.py` | `EvalContext` / `SpecialistHealthReport` / `DecisionLogRecord` |
| `eval/registry.py` | `EvalAdapter` Protocol + `SpecialistEvalRegistry` |
| `eval/bridge.py` | `EvalBridge` Protocol + `OrchestrationBridge`（★ **L2 调 L3 的唯一入口**） |
| `eval/facade.py` | `MultiagentProgrammaticFacade`（L3 未启用时用） |
| `eval/adapters/programmatic.py` | 最简评估器（先读） |
| `eval/adapters/llm_judge.py` | LLM 四维加权 |
| `eval/adapters/longitudinal_pairs.py` | 纵向配对 |
| `eval/decision_log.py` | 决策日志读写 |
| `eval/runtime_tracker.py` | 健康监控 |
| `eval/db.py` | L3 表 schema |
| `eval/bootstrap.py` | `setup_l3` 装配入口 |

---

#### 3. 重点看

**3.1. 一次派活的完整链路**

```
① LLM 决定派活
tool_call("delegate_to_codex", {goal, success_criteria})
    │
    ▼
② OrchestrationMiddleware.wrap_tool_call
    ├─ _before_handler
    │   ├─ set_current_state(state)              ← 写 ContextVar
    │   ├─ metrics.record_selection("codex")
    │   └─ metrics.record_invoked("codex")
    │
    └─ handler = delegate_to_codex（tools.py 闭包）
        │
        ├─ ① get_current_state()                 ← 读 ContextVar
        ├─ ② _extract_sandbox_id()
        ├─ ③ budget_guard.check_and_record()     ← 预算检查（L2）
        ├─ ④ ctx_summarizer.summarize()          ← 输入端闸门
        ├─ ⑤ specialist.invoke(request)
        │   └─ runtime.invoke(request)            ← 执行器
        ├─ ⑥ res_summarizer.summarize()          ← 输出端闸门
        ├─ ⑦ decision_log_writer.write_async()   ← L3 决策日志
        └─ ⑧ return json.dumps({...})
    │
    └─ _after_handler
        ├─ metrics.record_completion / fallback
        ├─ _build_orchestration_update → ArtifactRef 列表
        └─ return Command{update: {messages, orchestration}}
    │
    ▼
③ ThreadState 更新 → LLM 下一轮
```

**关键**：`OrchestrationMiddleware` 是**唯一入口**，所有 specialist 都从这里过。

**3.2. Specialist 与 Runtime 的关系**

| 组件 | 角色 | 管什么 | 不管什么 |
|---|---|---|---|
| `SpecialistAgent` | Agent 语义封装 | name / capabilities / invoke 转发 | 具体怎么跑 |
| `SpecialistRuntime` | 进程/协议执行器 | 裸执行（跑命令 / 调子进程） | 上下文摘要、结果压缩、凭证 |
| `CredentialProvider` | 身份提供者 | 发现凭证 | 刷新、存储、执行 |
| `ContextSummarizer` | 输入端闸门 | 压上下文 | 执行、结果 |
| `ResultSummarizer` | 输出端闸门 | 压结果 + 评估 | 执行、上下文 |

**Specialist 持有 Runtime（组合）**——`SubagentSpecialist._runtime` / `ClaudeCodeSpecialist._runtime`。**不是继承**。

**3.3. 四种运行时的横向对比**

| 维度 | subagent | claude_code | codex | pi |
|---|---|---|---|---|
| **Runtime 交互方式** | 进程内 `agent.invoke` | CLI 子进程 `subprocess.run` | ACP 异步 | `subprocess.Popen` + RPC |
| **通信方式** | 进程内 | stdout 一次性 | stdio JSON-RPC | stdio JSON-RPC |
| **凭证来源** | 继承 Leader | `~/.claude/.credentials.json` | `~/.codex/auth.json` | 13 个 env + config |
| **是否需要 installer** | 否 | 否 | 否 | **是（后台 npm install）** |
| **Context summarizer** | self_copy（LLM + 规则） | 纯规则（代码 + review） | 纯规则（代码 + 路径） | 纯规则（代码 + 路径） |
| **Result summarizer** | 纯继承 | 继承 + 2 覆写 | 继承 + 2 覆写 | **覆写 summarize** |
| **需要 sandbox bridge** | 否 | 否 | 否 | **是（TS extension）** |
| **capabilities** | RESEARCH | REVIEW | CODING | CODING |
| **token 用量** | ❌ | ❌ | ❌ | ✅ |
| **强制 MCP** | ❌ | ❌ | ❌ | ✅ |

**3.4. 双摘要器语义**

```
输入端 ContextSummarizer（进）：
    ThreadState → context_summary（字符串）
    目的：控制 token + 不污染 specialist

输出端 ResultSummarizer（出）：
    raw_output → SpecialistResult（含 success / summary / gap_analysis）
    目的：控制返回给 Leader 的 token + 程序化评估
```

**两者是独立契约**——per-specialist 各有实现。

**3.5. BaseResultSummarizer 是模板方法**

```python
class BaseResultSummarizer:
    def summarize(self, raw_output, artifacts, goal, success_criteria):
        success = self._evaluate_success(...)          # ① 校验（钩子）
        gap_analysis = "" if success else self._extract_gap(...)  # ② 提 gap（钩子）
        summary = self._compress(raw_output)            # ③ 压缩（钩子）
        failure_category = self._classify_failure(...)  # ④ 分类（钩子）
        return SpecialistResult(...)

    # 子类覆写钩子
    def _evaluate_success(self): ...
    def _extract_gap(self): ...
    def _compress(self): ...
    def _classify_failure(self): ...
```

**四个子类的差异**：

| 子类 | 覆写 | 特点 |
|---|---|---|
| `SelfCopyResultSummarizer` | 无 | 纯继承 |
| `ClaudeCodeResultSummarizer` | `_evaluate_success` + `_extract_gap` | +suggestion 检查 |
| `CodexResultSummarizer` | `_evaluate_success` + `_extract_gap` | +0 passed 检查 |
| `PiResultSummarizer` | `summarize`（整体） | 三段解析；**漏填 failure_category** |

**3.6. 装配链（三层嵌套）**

```
bootstrap_runtime（app 层）
  └─ setup_multiagent(config, agent_factory)
      ├─ metrics = MultiAgentMetricsStore(...)
      ├─ l2_setup = setup_l2(config, metrics)          ← L2 嵌套
      │   └─ l3_setup = setup_l3(config, metrics, task_queue)  ← L3 嵌套
      ├─ orch_mw = OrchestrationMiddleware(...)
      ├─ registry = SpecialistRegistry()
      ├─ for name in specialists_use:
      │     _load_specialist(name, config, agent_factory)
      │     ├─ "pi"       → PiInstaller + PiCredentialProvider + PiRuntime
      │     ├─ "codex"    → CodexCredentialProvider + CodexRuntime
      │     ├─ "claude"   → ClaudeCredentialProvider + ClaudeCodeRuntime
      │     └─ "subagent" → SubagentRuntime(agent_factory)
      │     register + make_specialist_tool
      ├─ if l2_setup: l2_setup.worker.start()
      └─ return MultiAgentSetup(...)
```

**三层 setup 对照**：

| 维度 | `setup_multiagent` | `setup_l2` | `setup_l3` |
|---|---|---|---|
| 开关 | `config.enabled`（默认 True） | `config.l2.enabled`（默认 False） | `config.l3.enabled`（默认 False） |
| 关闭时 | `_EMPTY_SETUP` | `None` | `None` |
| 产出 | `MultiAgentSetup` | `L2Setup` | `L3Setup` |

**3.7. L1/L2/L3 三层定位**

| 层 | 干什么 | 默认 | 触发方式 |
|---|---|---|---|
| **L1** | 每次派活记录指标 | **常开** | 每次调用 |
| **L2** | 读指标 → 变异模板 → 产新版本 | **关闭** | 数据驱动 |
| **L3** | 给 specialist 产出打分 | **关闭** | 数据驱动 |

**核心思想**：**便宜的事常做，贵的事按需做。**

**关键修正**：**编号不是执行顺序**——真实顺序是：

```
L2 触发进化 → L2 变异 → L3 评估 → L2 根据评估结果晋升
```

评估在"变异之后、晋升之前"。

**3.8. L2 演化在演化什么**

**演化的是两种模板**（不是 specialist 本身）：

| 产物 | 代号 | 控制什么 |
|---|---|---|
| `ContextSummaryTemplate` | **W2** | specialist 调用前的 context 生成 |
| `SkillInjectionTemplate` | **W4** | specialist 调用时的 skill 注入 |

**演化方向由 FailureCategory 驱动**：

- `CONTEXT_INSUFFICIENT` → 演化 W2
- `ABILITY_INSUFFICIENT` → 演化 W4
- `GOAL_UNCLEAR` / `SANDBOX_ISSUE` → **不演化**

**L2 不演化**：specialist 本身、Leader、L3（避免递归到 L4）。

**3.9. L2 与 L3 的接口位置**

**L2 → L3（唯一调用点）**：

```python
# PromotionGate.evaluate
if self._bridge is not None:
    ctx = EvalContext(candidate, baseline, task_sample)
    return self._bridge.evaluate(ctx)      # ← L3 的 OrchestrationBridge
```

**L3 → L2（两个回流点）**：

```python
# 回流点 A：L3 健康监控 enqueue L2 队列
cron_queue.put(("specialist_degraded", name))

# 回流点 B：L3 DecisionLog lessons 喂给 L2 变异
mutator.evolve_context_summary(current, failures, lessons=...)
```

**共享资源**：

| 资源 | L2 | L3 |
|---|---|---|
| `task_queue` | TriggerManager 写 / Worker 读 | trigger_l2_evolution_if_degraded 写 |
| `metrics_store` | metrics_view 读 | RuntimeTracker / DecisionLog 读写 |
| `l2_metrics` 表 | OrchestrationMetricsL2 写 | L3 事件加 `l3_` 前缀写同一张表 |
| `multiagent.db` | VersionDAG / BudgetGuard 读写 | L3SchemaManager 建表 / DecisionLog 读写 |

**3.10. 三个评估器**

| Adapter | method_used | 评分来源 | 分数形态 | 成本 |
|---|---|---|---|---|
| `ProgrammaticAdapter` | `"programmatic"` | evaluator → bool | 0/1 | 极低 |
| `LongitudinalPairsAdapter` | `"longitudinal_pairs"` | evaluator → bool | 0/1 | 低 |
| `LLMJudgeAdapter` | `"llm_judge"` | judge_fn → float | [0,1] 四维加权 | 高 |

**评估对象相同**（`EvolutionArtifact`：candidate vs baseline），**流程相同**（遍历 task_sample → 评分 → 算 Wilson CI），**唯一区别是"怎么给一个 task 打分"**。

**3.11. 两个 Facade（L3 未启用时的降级）**

| | OrchestrationBridge | MultiagentProgrammaticFacade |
|---|---|---|
| 接口 | `EvalBridge` | `EvalBridge`（同一个） |
| 启用条件 | L3 启用 | L3 未启用 |
| 评估方法 | 通过 registry 选（3 种） | 只支持 programmatic |
| 依赖 | `SpecialistEvalRegistry` | `Evaluator` callable |

**两者实现同一 `EvalBridge` 契约**——L2 调用方式完全一样。**这是"渐进迁移"设计**。

**3.12. 三个 Skill/Evolution 桥（最容易漏）**

| 桥 | 位置 | 作用 |
|---|---|---|
| `sandbox_binder.py` | multiagent 根目录 | multiagent ↔ sandbox 的桥 |
| `mcp/specialist_mcp_server.py` | multiagent/mcp/ | specialist 反向调 Poirot 8 沙箱接口 |
| `extensions/pi-sandbox-bridge/index.ts` | multiagent/extensions/ | Pi ↔ Poirot MCP 的 TS 跨语言桥 |

**3.13. 生命周期**

```
装配 → setup_multiagent(config, agent_factory)
    ├─ metrics = MultiAgentMetricsStore(...)
    ├─ l2_setup = setup_l2(...)               ← L2 装配
    │   └─ l3_setup = setup_l3(...)           ← L3 装配
    ├─ registry = SpecialistRegistry()
    ├─ for name in specialists_use:
    │     _load_specialist(name, config, agent_factory)
    │       ├─ PiInstaller.ensure_installed()（Pi 专属）
    │       ├─ XxxCredentialProvider.get_credential()（凭证探测）
    │       └─ XxxSpecialist(runtime=..., credential=...)
    │     registry.register(specialist)
    │     make_specialist_tool(name, ...) → tools
    └─ MultiAgentSetup(...)

使用 → Leader LLM 调 delegate_to_*
    └─ OrchestrationMiddleware.wrap_tool_call
        ├─ before：set_current_state + 打点
        ├─ handler：tools.py 编排
        │   ├─ budget_guard.check_and_record()
        │   ├─ ctx_summarizer.summarize()
        │   ├─ specialist.invoke → runtime.invoke
        │   └─ res_summarizer.summarize()
        └─ after：打点 + 归集 ArtifactRef

进化（L2，默认关）→ L2EvolutionWorker（daemon 线程）
    └─ _run_evolution(task)
        ├─ FailureFocuser.analyze → FailureStats
        ├─ VersionDAG.get_active → current
        ├─ EvolutionMutator.evolve_* → candidate
        ├─ PromotionGate.evaluate → ★ L3 调用点
        ├─ PromotionGate.decide → ACCEPT / REJECT / FAILED
        └─ VersionDAG.commit（accept/reject 都存）

评估（L3，默认关）→ bridge.evaluate(ctx)
    └─ OrchestrationBridge._select_method → adapter.evaluate

释放 → worker.stop() + shutdown_memory_worker（atexit）
```

**3.14. 三处关键安全 / 正确性设计**

| 编号 | 内容 |
|---|---|
| 契约隔离 | 7 个 Protocol，实现方无需继承，靠方法签名匹配 |
| 双异常层次 | `SpecialistError`（外部）和 `SubagentError`（内部）不共用基类 |
| fail-closed | 评估异常不抛，返回 `EvalResult(success=False)` |
| leaf role | 子 Agent 的 tool_groups 不含 multiagent，从工具层面杜绝递归 |
| hash 防环 | `hash_exists_in_recent` 近 5 版检查 |
| task 防过拟合 | 单 task 累计 ≤ 3 次 |
| 凭证不进 ThreadState | 只传 runtime，绝不写 state |
| 阻断机制 | 连续 3 次失败 → `record_blocked_marked`，24h 自动释放 |

---

#### 4. 不要看

- **ACP 协议的握手细节**（`conn.initialize` / `new_session` 的字段）——先建立"四条 runtime 各有各的通信方式"心智模型，细节留到调试时看。
- **pi 的 `--mode rpc` 事件格式细节**（`message_update` / `agent_end` / `extension_error`）——知道有这些事件即可。
- **Pi extension 的 JSON-RPC 细节**（`initialize` + `tools/call` 两条请求）——知道它桥接 pi 和 Poirot 即可。
- **`_serialize_payload` / `_deserialize_payload` 的字段映射**——知道"只存类名、反序列化为空 tuple"即可。
- **`_wilson_ci` 的公式推导**——知道它"小样本友好、p=0/1 不退化"即可。
- **`cli.py` 的 8 个 verb / 5 个 verb**——都是 skeleton，暂不实现，知道有即可。
- **`intent_strengthened.py` 的 LLM 兜底实现**——MVP 未实现，知道它是 `ContextSummarizer` 的输入源即可。
- **`evolution/types.py` 的 40 条 INVARIANT**——先看 `__init__.py` 的摘要，细节留到需要时查。

---

#### 5. 看完这一站应该能回答

1. Leader 什么时候决定"派活给子 Agent"，而不是自己干？
2. 一个 specialist 从被选中到返回结果，中间经历了哪些环节？
3. 子 Agent 的上下文从哪来？Leader 的完整对话历史会传给它吗？
4. 子 Agent 的结果怎么回到 Leader？是原文返回还是被摘要？
5. 多个子 Agent 能并行吗？它们的沙箱/凭证是共享还是隔离？
6. `Runtime` 和 `Specialist` 是什么关系？谁持有谁？
7. `Subagent` 和异构 `Specialist` 有什么区别？为什么是两条并列路径？
8. `eval` 和 `evolution` 是运行时的一部分，还是离线任务？
9. specialist 崩了/超时了，Leader 会感知吗？重试在哪层？
10. `L1/L2/L3` 是什么？为什么"L2 进化、L3 评估"不是执行顺序？
11. L2 演化在演化什么？为什么演化模板而不是 specialist 本身？
12. L3 评估是专门为 L2 服务的吗？
13. 三个评估器有什么区别？评估对象是什么？
14. `BaseResultSummarizer` 是什么模式？为什么 Pi 覆写了 `summarize` 而其他只覆写钩子？
15. 七个契约分别是什么？为什么全部用 Protocol？
16. 为什么 Pi 需要 installer 而 Claude/Codex 不需要？
17. 三个桥分别是什么？为什么最容易漏？
18. 三层装配是怎么嵌套的？（setup_multiagent → setup_l2 → setup_l3）
19. 两个 Facade 为什么实现同一契约？
20. L2 与 L3 的接口在哪两处？

---

#### 6. 对照

- `bootstrap.py` 的 `setup_multiagent` ↔ `_load_specialist` + `registry.register` + `make_specialist_tool`。
- `middleware.py` 的 `wrap_tool_call` ↔ `_before_handler` / `_after_handler` / `_make_error_response`。
- `tools.py` 的 `make_specialist_tool` ↔ `@tool(f"delegate_to_{name}")` + 闭包捕获。
- `OrchestrationMiddleware` 的 `set_current_state` ↔ `tools.py` 的 `_current_state`（ContextVar）。
- `XxxSpecialist.invoke` ↔ `XxxRuntime.invoke`（纯转发）。
- `XxxContextSummarizer.summarize` ↔ Sandbox 编排的"输入端闸门"。
- `XxxResultSummarizer.summarize` ↔ `BaseResultSummarizer.summarize`（模板方法）+ 子类钩子。
- `evolution/bootstrap.py` 的 `setup_l2` ↔ `setup_l3`（嵌套调用）。
- `promotion_gate.py` 的 `evaluate` ↔ `bridge.evaluate(ctx)`（★ L2 调 L3）。
- `runtime_tracker.py` 的 `trigger_l2_evolution_if_degraded` ↔ `cron_queue.put(...)`（L3 调 L2）。
- `version_dag.py` 的 `get_active` ↔ L1 每次派活时读活跃模板（hot swap）。
- `budget_guard.py` 的 `check_and_record` ↔ `tools.py` 的 `delegate_tool` handler（派活前预算检查）。
- `metrics.py` 的 `record_selection` / `record_invoked` / `record_completion` / `record_fallback` ↔ `OrchestrationMiddleware` 的 before/after。
- `pi_installer.py` 的 `ensure_installed` ↔ `setup_multiagent` 的 `_load_specialist("pi", ...)`。
- `pi-sandbox-bridge/index.ts` 的 `callPoirotMcp` ↔ `mcp/specialist_mcp_server.py` 的 8 接口。

---

**总览**：

```
契约（7 个）→ 装配（setup_multiagent）→ 四 specialist + 四 runtime → 接入 Leader
   ①              ②                        ③④                     ⑤
                                              ↓
                            L1 记录 → L2 变异 → L3 评估 → L2 晋升
                                             ⑥
```

**multiagent 的本质**：一个"把活派给别人做"的编排层——`OrchestrationMiddleware` 拦截 `delegate_to_*`，`tools.py` 编排"摘要 → 执行 → 摘要"，`Specialist` 持有 `Runtime` 决定"怎么跑"，`CredentialProvider` 决定"以谁的身份跑"，两个 `Summarizer` 决定"进出的信息怎么压缩"。四个 specialist 是四种接入方式：**subagent（进程内自复制）+ claude（CLI 子进程）+ codex（ACP 协议）+ pi（RPC + TS 扩展）**。三层学习体系构成闭环：**L1 记录（常开）→ L2 变异（默认关）→ L3 评估（默认关）→ L2 晋升**。L2 演化的是两种模板（W2 / W4），不是 specialist 本身；L3 是"打分员"，为 L2 的晋升决策提供评估。两座桥（`sandbox_binder` + `specialist_mcp_server`）+
### 第 8 站：`reporting/`（报告）

**为什么最后**：报告是**输出层**，依赖前面所有组件。

**看什么**：

| 文件                     | 看什么           |
| ---------------------- | ------------- |
| `markdown_reporter.py` | Markdown 报告生成 |
| `result.py`            | 报告结果          |
| `thread_report.py`     | 线程报告          |

**重点看**：

- `generate_report_from_thread` 怎么从 state 生成报告；
- `ReportMiddleware` 和 `reporter` 的**分工**。

---

## 三、支撑层

`config` / `prompts` / `runtime` / `state` / `observability` / `journal` 这些**不用专门看**，遇到时顺手看：

| 场景 | 看什么 |
|---|---|
| 遇到配置项不懂 | `config/loader.py` + `config/schema.py` |
| 遇到 prompt 不懂 | `prompts/manager.py` + `prompts/system/leader/*.md` |
| 遇到 state 字段不懂 | `state/thread_state.py` + `state/types.py` |
| 遇到日志不懂 | `journal/run_journal.py` + `observability/` |
| 遇到 checkpointer 不懂 | `runtime/checkpointer.py` |

---

## 四、推荐的“一次对话”路径

```
1. 用户提问
   ↓
2. main.py → handle_command / _run_chat_async      
   ↓
3. bootstrap_runtime → make_lead_agent             
   ↓
4. graph.astream
   ↓
5. before_agent 中间件链
   - system_context_middleware.py                  ← 第 1 站
   - context_engineering/strategies/default/       ← 第 2 站
   - memory_recall_middleware.py                   ← 第 4 站
   - skill_injection_middleware.py                 ← 第 5 站
   ↓
6. agent 节点
   - model.invoke()
   - tool_calls
   ↓
7. tools 节点
   - agent_tools/builtin/                          ← 第 3 站
   - sandbox/integration/tools.py                  ← 第 6 站
   - multiagent/tools.py                           ← 第 7 站
   ↓
8. after_agent 中间件链
   - reflection_middleware.py                      ← 第 1 站
   - report_middleware.py                          ← 第 8 站
   - memory_consolidation_middleware.py            ← 第 4 站
   ↓
9. final_state → CLI/TUI 渲染
```

**按这个顺序看，每个组件都能落到“它在哪一步、解决什么问题”。**

---

## 五、每个组件看什么、不看什么

| 组件 | 重点看 | 不看 |
|---|---|---|
| middlewares | 四个钩子、state 传递、控制流 | 每个中间件的业务细节 |
| context_engineering | budget、compaction、custom 事件 | 具体压缩算法 |
| agent_tools | `@tool` 机制、注册、group | 每个工具的实现细节 |
| memory | 召回/提取时机、worker 异步 | 存储后端细节 |
| skill | `SKILL.md` 格式、选中、注入 | 进化算法 |
| sandbox | 工具路由、local vs docker | Docker API 细节 |
| multiagent | delegate、结果摘要、leaf 限制 | 各 runtime 细节 |
| reporting | 报告合成、middleware 分工 | Markdown 模板细节 |

---

## 六、看组件时问自己三个问题

每看一个组件，问：

1. **它在哪一步被调用？**（before_agent / wrap_model_call / wrap_tool_call / after_agent）
2. **它读写哪些 state 字段？**（messages / governance / sandbox / todos / ...）
3. **它失败时会怎样？**（抛异常 / 降级 / 跳过）

---

## 七、总结

>  **入口 → 装配 → 执行** 三层实现循环；  
> 组件层：  
> **middlewares → context_engineering → agent_tools → memory → skill → sandbox → multiagent → reporting**；  
> 每个组件问三个问题：**在哪被调、读写什么 state、失败怎么办**；  
> 支撑层（config / prompts / state / journal）遇到时顺手看。

---

