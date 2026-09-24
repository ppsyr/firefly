# Graph 的 State 到底是什么

```
State（ThreadState）
    = 图执行期间的"共享内存"
    = 所有节点读写的唯一数据源
    = 跨轮 + 跨模式持久化（经 checkpointer）
```

---


## 1. State 里含有什么

`ThreadState` 继承 `AgentState`（自带 `messages`），包含以下字段（按用途分类）：

| 层 | 是什么 | 谁看 | 用途 |
|---|---|---|---|
| **物理层** | `messages` 列表（System / Human / AI / Tool） | **LLM** | 模型唯一能"读"的输入 |
| **逻辑层** | 一整个 `ThreadState`（含 messages + 10+ 其他字段） | **代码**（节点 / 中间件 / 工具） | 逻辑决策的数据源 |

### 1.1. 对话类（直接进 LLM）

| 字段                           | 作用                               | 由谁写                        |
| ---------------------------- | -------------------------------- | -------------------------- |
| `messages`                   | 对话历史（System / Human / AI / Tool） | LangGraph 自动 append + 各中间件 |
| `research_question`          | 研究问题 → `<goal>` 标签               | 初始 state + 各中间件            |
| `todos`                      | 待办列表 → `<plan>` 标签               | `TodoMiddleware`           |
| `reflection_items`           | 反思项 → `<reflection>` 标签          | `ReflectionMiddleware`     |
| `governance.default.summary` | 上下文摘要 → `<summary>` 标签           | 治理策略                       |

### 1.2. 证据类（reducer 合并，元素为 frozen dataclass）

| 字段 | 作用 | reducer |
|---|---|---|
| `observations` | 观察（证据条目） | `merge_observations`（追加） |
| `sources` | 来源 | `merge_sources`（id/url 去重） |
| `citations` | 引用 | `merge_citations`（id 合并） |
| `artifacts` | 产物 | `merge_artifacts`（id 合并） |

### 1.3. 输出类

| 字段 | 作用 | reducer |
|---|---|---|
| `final_report` | 最终报告 | `merge_final_report`（last-write-wins） |

### 1.4. 旁路元数据（默认不进 LLM）

| 字段 | 作用 | 谁看 |
|---|---|---|
| `metadata.active_skills` | skill provenance 锚点 | `SkillMetricsMiddleware` 归因 |
| `metadata.skill_applied` | skill 是否被应用 | 同上 |
| `metadata.title` | run 标题 | UI / 列表展示 |
| `metadata.system_context` | agent 身份 + 时区 | 下游 prompt 拼装 / trace |
| `skill_suggestion` | 主动建议的 skill | agent 自己决定是否用 |
| `errors` | 错误账本 | `_judge_task_completed` 判定 |
| `tagged_context` | 渲染快照 | trace / 审计 |

### 1.5. 控制 / 状态类

| 字段 | 作用 | reducer |
|---|---|---|
| `user_input` | 用户原始输入 | 初始 state |
| `intent` | 意图 | 意图引擎 |
| `plan` | 研究计划 | 各中间件 |
| `current_step_id` | 当前步骤 | `TodoMiddleware` |
| `governance` | 治理层状态（策略 bundle 自管命名空间） | `merge_governance`（deep-merge） |
| `sandbox` | 沙箱状态（sandbox_id） | `merge_sandbox`（fail-closed） |
| `orchestration` | 多 Agent 编排状态 | `merge_orchestration`（去重追加） |
| `recalled_memories` | 召回的记忆索引 | `merge_memory_recalled`（id 去重） |
| `memory_updates` | 记忆更新 | `merge_memory_updates`（追加） |
| `help_request_count` | 求助次数 | `StallDetectionMiddleware` |
| `pending_help` | 待处理求助 | HITL |

---

## 4. 每一部分有什么用

### 4.1. 对话类 → 喂 LLM

```
messages ────────────────┐
research_question ──┐    │
todos ──────────────┤    │   ┌─ TaggedContextMiddleware._assemble
reflection_items ───┤    ├──▶│   · 头部字段 → SystemMessage(context_block)
governance.summary ─┘    │   │   · messages → 改写后对话历史
原 SystemMessage ────────┘   └─ request.override(messages=[...])
                                        │
                                        ▼
                                    ┌─────────┐
                                    │   LLM   │
                                    └─────────┘
```

**这一组是"LLM 真正看到的内容"**——由 `TaggedContextMiddleware._assemble` 渲染成：
- **头部上下文块**（`<goal><plan><reflection><summary><date><system>`）→ SystemMessage；
- **对话历史**（`messages` 改写为 `<thinking><answer><toolresult>` 序列）。

### 4.2. 证据类 → 沉淀证据

```
工具调用（web_search / browse / read_snapshot）
    └─ EvidenceMiddleware.wrap_tool_call
        ├─ _extract_sources → sources
        ├─ _make_observation → observations
        └─ 双写 messages（模型可见）+ 旁路存档
```

**这一组是"研究过程中积累的证据"**——被 `EvidenceMiddleware` 结构化沉淀，供报告合成 / 反思判断。

### 4.3. 输出类 → 最终产物

```
final_report
    ├─ ReportMiddleware.after_agent（expert 模式写）
    └─ Reporter.generate_report（default 回退）
```

**这一组是"运行的结果产物"**——`ReportMiddleware` 在 expert 模式合成，或由 reporter 兜底。

### 4.4. 旁路元数据 → 代码逻辑用

```
metadata.active_skills ──┐
metadata.skill_applied ──┤  ┌─ SkillMetricsMiddleware（归因）
metadata.title ──────────┼──┤  ┌─ TitleMiddleware（展示）
metadata.system_context ─┤  ├──┤  ┌─ 下游 prompt 拼装
skill_suggestion ────────┤  │  │  └─ agent 自己决定用不用
errors ──────────────────┤  │  └─ ToolCallMiddleware（判定成败）
tagged_context ──────────┘  └─ 审计 / trace
```

**这一组"不给 LLM 看，只给代码用"**——provenance / 展示 / 判定 / 审计。

### 4.5. 控制 / 状态类 → 图执行控制

```
governance ──┐
sandbox ─────┤  ┌─ 各中间件读写，控制执行行为
orchestration┤──┤
recalled_memories / memory_updates ──┘
help_request_count / pending_help ──── 控制求助 / 暂停
```

**这一组是"图执行的控制信号"**——决定 sandbox 复用、记忆注入、多 Agent 派活、求助暂停。

---

## 5. State 的生命周期

### 5.1. 创建（一次 run 开始）

```
create_initial_thread_state(user_input)
    ├─ messages: []
    ├─ user_input
    ├─ observations / sources / citations / artifacts / reflection_items / errors: []
    ├─ metadata: {}
    ├─ governance / sandbox / orchestration / recalled_memories / memory_updates: None
    └─ 返回 ThreadState
```

### 5.2. 演进（图执行中）

```
每个节点 / 中间件读写 state
    └─ 字段级 reducer 合并（merge_xxx）
        ├─ 列表类 → 追加 / 去重追加
        ├─ 覆盖类 → final_report / tagged_context
        ├─ dict 类 → metadata（浅合并）/ governance（deep-merge）
        └─ 特殊类 → sandbox（fail-closed）/ orchestration（去重追加）
```

### 5.3. 持久化（跨轮 + 跨模式）

```
checkpointer（InMemorySaver 单例）
    └─ 按 thread_id 存取 state
        ├─ 跨轮：同一 thread 的多次 run 累积 state
        └─ 跨模式：switch_expert_mode 复用 thread_id → state 保留
```

### 5.4. 收口（run 结束）

```
LeaderAgent.run / PoirotStreamClient.stream
    └─ 从 final_state 取 final_report / 最后 AIMessage
        └─ 返回给上层（CLI / API）
```

---

## 6. State 与其他概念的关系

```
State（ThreadState）        ← 图内共享内存
    │
    ├─ 被谁写
    │     ├─ 节点（agent / tools / ...）
    │     ├─ 中间件（before/after/wrap 各阶段）
    │     └─ 工具（返回 ToolMessage → append messages）
    │
    ├─ 被谁读
    │     ├─ 中间件（读 state 决定注入什么）
    │     ├─ TaggedContextMiddleware（渲染成 messages 送 LLM）
    │     └─ LeaderAgent.run（取 final_report）
    │
    ├─ 存到哪
    │     └─ checkpointer（按 thread_id）
    │
    └─ 与 RunContext 的区别
          ├─ State：图执行状态（LangGraph 管，自动合并）
          └─ RunContext：运行元信息（run_id / journal / output_dir）
```

**关键区分**：
- **`ThreadState`** → 图内状态，LangGraph 管理合并，跨轮累积；
- **`RunContext`** → 运行元信息（run_id / journal / output_dir / config），由 `RunManager` 管理；
- **`RunRecord`** → 运行记录（状态快照），落盘 `record.json`；
- **`RunEvent`** → 运行事件（事件流），落盘 `events.jsonl`。

---

## 7. 一张图收束全局

```
┌─────────────────────────────────────────────────────────────────┐
│                       ThreadState（总容器）                       │
│                                                                 │
│  ┌─ 对话类（进 LLM）─────────────────────────┐                    │
│  │  messages / research_question / todos /   │                    │
│  │  reflection_items / governance.summary    │                    │
│  └──────────────────┬────────────────────────┘                    │
│                     │                                              │
│                     ▼                                              │
│            TaggedContextMiddleware._assemble                       │
│                     │                                              │
│                     ├─ 头部字段 → SystemMessage(context_block)     │
│                     └─ messages → 改写后对话历史                    │
│                     │                                              │
│                     ▼                                              │
│                 request.override(messages=[...])                   │
│                     │                                              │
│                     ▼                                              │
│                 ┌─────────┐                                        │
│                 │   LLM   │                                        │
│                 └────┬────┘                                        │
│                      │                                             │
│                      ▼                                             │
│              AIMessage（content / tool_calls / reasoning_content） │
│                      │                                             │
│                      ▼                                             │
│              append 回 state["messages"]                           │
│                                                                    │
│  ┌─ 证据类（沉淀）───┐   ┌─ 输出类 ────┐   ┌─ 旁路元数据（代码用）─┐  │
│  │ observations     │   │ final_report│   │ active_skills         │  │
│  │ sources          │   │             │   │ title / system_context│  │
│  │ citations        │   │             │   │ skill_suggestion      │  │
│  │ artifacts        │   │             │   │ errors / tagged_context│  │
│  └──────────────────┘   └─────────────┘   └───────────────────────┘  │
│                                                                    │
│  ┌─ 控制 / 状态类 ────────────────────────────────────────────┐     │
│  │ governance / sandbox / orchestration / recalled_memories /  │     │
│  │ memory_updates / help_request_count / pending_help          │     │
│  └─────────────────────────────────────────────────────────────┘     │
└─────────────────────────────────────────────────────────────────┘
                            │
                            ▼
                     checkpointer（按 thread_id 持久化）
```

---

## 8. 总结

> **State（`ThreadState`）是 LangGraph 在整张图执行过程中携带的"总容器"——图内唯一的共享内存，跨节点 / 跨中间件 / 跨轮的唯一数据通道。它含五类字段：对话类（进 LLM）、证据类（沉淀）、输出类（产物）、旁路元数据（代码逻辑用）、控制 / 状态类（图执行控制）。关键区分是"物理层（messages，LLM 只吃这个）"与"逻辑层（整个 state，代码用）"——state 其他字段不自动送 LLM，必须由中间件显式渲染进 messages 或 SystemMessage 才会被送。LLM 只回一条 AIMessage（content / tool_calls / reasoning_content），append 回 messages，下一轮再被渲染送出去。**