---

name: Memory 辅助类型：MemoryQuery / MemoryFilter / RetrievalResult。

description:

【整体职责】
定义 memory 模块的辅助数据类型：检索入参、遗忘过滤条件、检索出参。
是 memory 模块的「数据底座」之一，被 retriever / store / manager 共享。

【组成】
1. MemoryQuery（frozen）    ：检索查询（retriever.retrieve 入参）。
2. MemoryFilter（frozen）   ：记忆过滤（forget_policy / store.list_by_filter 用）。
3. RetrievalResult（frozen）：检索结果（retriever.retrieve 出参）。

【职责边界】
- 本模块只定义数据模型，不含检索逻辑（检索在 retriever）。
- RetrievalResult.compute_score 是唯一的方法，只做纯计算。
- 不 import 项目内其他模块（除 schema 的类型）。

【INVARIANT】
- RetrievalResult.score 复合分数公式 score = similarity × 0.7 + strength × 0.3，
  语义相关性占主导（70%），记忆强度参与排序（30%）。

---


## 0. 结构树

```
types.py —— Memory 辅助类型：MemoryQuery / MemoryFilter / RetrievalResult
│
│   整体职责：定义 memory 模块的辅助数据类型：检索入参、遗忘过滤条件、检索出参。
│             是 memory 模块的「数据底座」之一，被 retriever / store / manager 共享。
│
│   核心约定：三个类型都是 frozen dataclass，不可变。
│
│
├── 导入的模块
│   │
│   └── schema.py · MemoryTrace / MemoryType
│         被 RetrievalResult（trace 字段）和 MemoryQuery / MemoryFilter（type_filter）引用。
│
│
├── MemoryQuery（frozen dataclass）—— 检索查询
│   │   检索入参（retriever.retrieve 用）。
│   │
│   └── 字段
│       ├── text : str                        ← 查询文本
│       ├── top_k : int = 5                   ← 返回条数上限
│       ├── type_filter : MemoryType | None   ← 按类型过滤（None=不限）
│       ├── min_strength : float = 0.0        ← 最低强度门槛
│       └── metadata_filter : dict = {}       ← 按 metadata 过滤（全匹配）
│
│   被谁引用：
│       ├── retriever.py · retrieve(query)（读全部字段）
│       └── memory_recall_middleware.py · abefore_model（构造 query）
│
│
├── MemoryFilter（frozen dataclass）—— 遗忘过滤
│   │   记忆过滤（遗忘策略 / store.list_by_filter 用）。
│   │   语义：store 只按 type / max_age_hours / metadata 粗筛（内存索引），
│   │         strength 精算由调用方（forget_policy）逐条 compute_strength。
│   │
│   └── 字段
│       ├── type_filter : MemoryType | None   ← 按类型过滤（None=不限）
│       ├── min_strength : float = 0.0        ← 最低强度门槛（store 不精算，透传参考）
│       ├── max_age_hours : float | None      ← 最大年龄（小时，None=不限）
│       └── metadata_filter : dict = {}       ← 按 metadata 过滤（全匹配）
│
│   被谁引用：
│       └── store.py · list_by_filter(filter)（读 type / max_age_hours / metadata）
│
│
└── RetrievalResult（frozen dataclass）—— 检索结果
    │   检索出参（retriever.retrieve 用）。
    │
    ├── 字段
    │   ├── trace : MemoryTrace        ← 命中的 trace（可能已强化）
    │   ├── similarity : float         ← 语义相似度 0.0~1.0
    │   ├── strength : float           ← 当前强度（retrieve 时算）
    │   └── score : float              ← 复合分数 = similarity × 0.7 + strength × 0.3
    │
    ├── compute_score(trace, similarity, strength) → RetrievalResult
    │   │   classmethod：构造 RetrievalResult 并算复合分数。
    │   │
    │   ├── 做什么
    │   │   ├── score = similarity × 0.7 + strength × 0.3
    │   │   └── cls(...) 构造并返回
    │   │
    │   └── 引用的模块
    │       └── MemoryTrace（同文件入参）
    │
    └── 被谁引用：
        ├── retriever.py · retrieve（构造结果 / 读 score / strength / trace）
        └── memory_recall_middleware.py · _format_recall（读 score / strength / trace.content）
```

---

## 1. 三个类型的用途

```
MemoryQuery       = 检索入参
                    │
                    └── retriever.retrieve(query)
                            ├── text → BM25 算分
                            ├── top_k → 结果截断
                            ├── type_filter → 候选过滤
                            ├── min_strength → 强度门槛
                            └── metadata_filter → 候选过滤（预留）


MemoryFilter      = 遗忘过滤
                    │
                    └── store.list_by_filter(filter)
                            ├── type_filter → 按类型粗筛
                            ├── max_age_hours → 按年龄粗筛（store 唯一做的事）
                            ├── metadata_filter → 全匹配过滤
                            └── min_strength → 不在此精算（留给 forget_policy）


RetrievalResult   = 检索出参
                    │
                    └── retriever.retrieve 返回 list[RetrievalResult]
                            ├── trace → 命中的记忆
                            ├── similarity → BM25 相似度
                            ├── strength → 当前强度
                            └── score → 排序分（similarity × 0.7 + strength × 0.3）
```

---

## 2. 复合分数公式

```
score = similarity × 0.7 + strength × 0.3

├── similarity：语义相关性（BM25 相似度）  → 70%
└── strength：记忆强度（Ebbinghaus 衰减）  → 30%

含义：语义相关性占主导，记忆强度参与排序。
```

---

## 3. 被谁引用

```
MemoryQuery
├── retriever.py · retrieve(query)
└── memory_recall_middleware.py · abefore_model

MemoryFilter
└── store.py · list_by_filter(filter)

RetrievalResult
├── retriever.py · retrieve（构造 + 返回）
└── memory_recall_middleware.py · _format_recall（读字段）
```

---

## 4. 总览

```
MemoryQuery     = 检索入参（text / top_k / type_filter / min_strength / metadata_filter）
MemoryFilter    = 遗忘过滤（type_filter / min_strength / max_age_hours / metadata_filter）
RetrievalResult = 检索出参（trace / similarity / strength / score）
compute_score   = classmethod：score = similarity × 0.7 + strength × 0.3
```