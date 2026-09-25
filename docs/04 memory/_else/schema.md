---

name: MemoryTrace — 记忆原子单元（frozen dataclass）。

description:

【整体职责】
定义记忆的数据模型：记忆类型、关联、操作日志、记忆痕迹本身。
是 memory 模块的「数据底座」，被 schema 消费方（store / retriever / manager /
中间件 / worker）全部共享。

【组成】
1. MemoryType（Enum）    ：记忆类型（认知科学映射）。
2. Association（frozen） ：记忆关联（扩散激活用）。
3. OperationLog（frozen）：操作日志条目（traceability，debug 用）。
4. MemoryTrace（frozen） ：记忆痕迹本身，模块的核心数据类。

【职责边界】
- 本模块只定义数据模型，不含任何逻辑（除 with_strength / with_operation 两个派生方法）。
- 不 import 项目内其他模块（除标准库），是纯数据层。
- 所有「修改」都通过 replace() 派生新实例，不原地修改（frozen 语义）。

【INVARIANT】
- MemoryTrace 不可变：strength 等可变字段通过 with_strength() / with_operation()
  创建新实例替换（类似 skill version DAG 的 is_active 指针）。
- operation_log 上限 20 条 FIFO；retrieve 不记（高频，强化在 strength / access_count
  已体现）；actor 字段预留 turn_id（Layer 4 Middleware 注入）。

---

## 0. 结构树

```
schema.py —— MemoryTrace，记忆原子单元（frozen dataclass）
│
│   整体职责：定义记忆的数据模型：记忆类型、关联、操作日志、记忆痕迹本身。
│             是 memory 模块的「数据底座」，被 store / retriever / manager /
│             中间件 / worker 全部共享。
│
│   核心约定：所有「修改」都通过 replace() 派生新实例（frozen 语义）。
│
│
├── 导入的模块
│   └── （无项目内引用，纯数据层，只用标准库 dataclasses / enum / typing）
│
│
├── MemoryType（Enum）—— 记忆类型
│   │   认知科学映射。
│   │
│   └── 枚举值
│       ├── EPISODIC   = "episodic"     ← 事件记忆，衰减快，需反复检索强化
│       ├── SEMANTIC   = "semantic"     ← 语义记忆，衰减慢，稳定知识
│       └── PROCEDURAL = "procedural"   ← 过程记忆，几乎不衰减，技能
│
│   被谁引用：
│       ├── manager.py · _compute_trace_id / _get_decay_params / encode / consolidate
│       ├── store.py · _parse_trace / list_by_type
│       ├── retriever.py · retrieve（type_filter）
│       ├── worker.py · MemoryType(...)
│       └── decay.py · _get_decay_params
│
│
├── Association（frozen dataclass）—— 记忆关联
│   │   扩散激活用：两条记忆之间的有向连接。
│   │   retrieve 命中一条时，可沿 associations 扩散激活相关记忆（Layer 6 图检索）。
│   │
│   └── 字段
│       ├── target_id : str       ← 目标记忆 id
│       ├── strength : float = 0.5 ← 关联强度 0.0~1.0
│       └── type : str = "related" ← 关联类型（related / causal / temporal / contrast）
│
│   被谁引用：
│       ├── manager.py · associate / _add_association_with_lru / consolidate
│       └── store.py · _parse_trace / _serialize_trace
│
│
├── OperationLog（frozen dataclass）—— 操作日志条目
│   │   traceability / debug 用。
│   │   每次 manager 操作 append 一条；retrieve 不记（高频）。
│   │
│   └── 字段
│       ├── timestamp : float           ← 操作发生时间（unix timestamp）
│       ├── operation : str             ← 操作名（encode/associate/consolidate/reconsolidate/forget）
│       ├── actor : str | None = None   ← 操作者（thread_id / turn_id，Layer 4 注入）
│       └── diff : dict | None = None   ← 变更摘要（如 content 前后对比）
│
│   被谁引用：
│       ├── manager.py · encode / associate / consolidate / reconsolidate
│       └── store.py · _parse_trace / _serialize_trace / _diff_to_serializable
│
│
└── MemoryTrace（frozen dataclass）—— 记忆痕迹（核心数据类）
    │   frozen 语义：strength 等可变字段通过 with_strength / with_operation 派生新实例。
    │   traceability：operation_log 上限 20 条 FIFO。
    │
    ├── 字段（按分组）
    │   │
    │   ├── 身份
    │   │   ├── id : str                ← 唯一 id（= SHA256(content+type)[:16]）
    │   │   ├── content : str           ← 记忆正文
    │   │   └── type : MemoryType       ← 记忆类型
    │   │
    │   ├── 强度与衰减
    │   │   ├── strength : float = 0.0        ← 当前强度（lazy decay，retrieve 时算）
    │   │   ├── base_strength : float = 0.7   ← 初始强度（由 type 决定）
    │   │   ├── decay_rate : float = 0.1      ← 衰减速率
    │   │   ├── access_count : int = 0        ← 累计访问次数
    │   │   ├── last_accessed : float = 0.0   ← 最后访问时间
    │   │   └── importance : float = 0.5      ← 语义重要性 0.0~1.0
    │   │
    │   ├── 关联
    │   │   └── associations : tuple[Association, ...]  ← 扩散激活用
    │   │
    │   ├── 预留
    │   │   └── embedding : tuple[float, ...] | None    ← 向量库阶段填充
    │   │
    │   ├── 溯源
    │   │   ├── source : str | None = None            ← 来源（thread_id / run_id / user_input）
    │   │   ├── created_at : float = 0.0              ← 创建时间
    │   │   └── metadata : dict[str, Any] = {}        ← tags / project / specialist_id
    │   │
    │   └── 审计
    │       └── operation_log : tuple[OperationLog, ...]  ← 上限 20 条 FIFO
    │
    ├── with_strength(new_strength, accessed_at) → MemoryTrace
    │   │   检索强化时用（frozen 语义，派生新实例）。
    │   │
    │   ├── 做什么
    │   │   ├── strength 替换为 new_strength
    │   │   ├── access_count + 1
    │   │   └── last_accessed 替换为 accessed_at
    │   │
    │   └── 引用的模块
    │       └── Association 无关（只操作自身字段）
    │
    └── with_operation(log, *, max_log=20) → MemoryTrace
        │   append 一条操作日志（frozen 语义，派生新实例）。
        │
        ├── 做什么
        │   ├── operation_log + 新 log（tuple 拼接）
        │   ├── 超 max_log → 保留最近 max_log 条（切片 [-max_log:]）
        │   └── replace 派生新实例（只改 operation_log）
        │
        └── 引用的模块
            └── OperationLog（同文件）
```

---

## 1. 被谁引用

```
MemoryType
├── manager.py · _compute_trace_id / _get_decay_params / encode / consolidate
├── store.py · _parse_trace / list_by_type
├── retriever.py · retrieve（type_filter）
├── worker.py · MemoryType(...)
└── decay.py · _get_decay_params

Association
├── manager.py · associate / _add_association_with_lru / consolidate
└── store.py · _parse_trace / _serialize_trace

OperationLog
├── manager.py · encode / associate / consolidate / reconsolidate（构造日志）
└── store.py · _parse_trace / _serialize_trace / _diff_to_serializable

MemoryTrace
├── manager.py · 四操作构造 / 派生 trace
├── store.py · _parse_trace 构造 / _serialize_trace 读字段
├── retriever.py · retrieve 读字段 / with_strength 强化
├── memory_recall_middleware.py · 读 trace.id / score / strength
└── worker.py · 读 content / created_at / metadata.forgotten
```

---

## 2. frozen 语义

```
MemoryTrace 的所有「修改」都通过 replace 派生新实例：

with_strength(new_strength, accessed_at)
    └── replace(self, strength=..., access_count=+1, last_accessed=...)

with_operation(log, max_log=20)
    └── replace(self, operation_log=(...+log,)[-max_log:])

好处：
├── 线程安全（不可变对象）
├── 便于回溯（每次变更都产生新实例）
└── 类似 skill version DAG 的 is_active 指针
```

---

## 3. 总览

```
MemoryType       = 记忆类型枚举（EPISODIC / SEMANTIC / PROCEDURAL）
Association      = 记忆关联（target_id / strength / type）
OperationLog     = 操作日志条目（timestamp / operation / actor / diff）
MemoryTrace      = 记忆痕迹（17 字段，frozen，模块核心数据类）
with_strength    = 检索强化派生（strength + access_count + last_accessed）
with_operation   = append 操作日志（上限 20 条 FIFO）
```