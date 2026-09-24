"""Thread state package — 线程状态模块。

【整体职责】
定义 Poirot 研究线程在 LangGraph 图中的状态：schema 与承载类型、字段合并规则、
以及初始状态工厂。是图内所有节点读写状态的统一契约来源。

【内容摘要】
- types          : 状态 schema 与承载类型（ThreadState + Observation/Source/Artifact 等 frozen dataclass）。
- reducers       : 字段合并规则（merge_thread_state 外层入口 + merge_xxx 字段级 reducer）。
- thread_state   : 初始状态工厂（create_initial_thread_state）。

【职责边界】
- 只负责：状态 schema、承载类型、合并规则、初始状态构造。
- 不负责：状态的持久化（runtime / checkpointer）、使用状态的业务逻辑
  （middleware / agent / tool）、状态变更的调度（graph）。

【三层结构】
- schema 层：types（ThreadState 字段以 Annotated[..., merge_xxx] 绑定 reducer）。
- 规则层  ：reducers（实现合并语义：追加 / 去重 / 覆盖 / deep-merge / fail-closed）。
- 工厂层  ：thread_state（产出初始 state，不参与 reducer 绑定）。

【两套 reducer 入口】
- 字段级：merge_xxx，供 Annotated 绑定，由 LangGraph 图内触发。
- 外层  ：merge_thread_state，供 LeaderAgent 手工调用，内部分派到规则。
"""