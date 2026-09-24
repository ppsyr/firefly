"""L3 eval 编排评估层 — 可插拔评估方法库 + 健康监控 + 跨 run 学习。

【整体职责】
L3 是 multiagent 的评估层：接收 L2 的评估请求（candidate vs baseline + task_sample），
用可插拔的评估方法（EvalAdapter）打分，返回 EvalResult 供 L2 晋升决策；
同时提供 specialist 健康监控与跨 run 决策日志累积。

【内容摘要】
- OrchestrationBridge : L2 调 L3 的唯一入口（evaluate → EvalResult）。
- EvalAdapter         : 可插拔评估方法库（programmatic / llm_judge / longitudinal_pairs）。
- RuntimeTracker      : specialist 健康监控 + degraded 检测。
- DecisionLog         : 跨 run lessons 累积（异步写 + 归档）。

【职责边界】
- 只负责：评估打分、健康监控、决策日志读写。
- 不负责：进化变异与晋升决策（L2 负责）、指标打点（L1 负责）、
  task_sample 抽样（L2 负责）。
- 不进 L1 graph：与 L2 一致，独立于 L1 主执行链。

【INVARIANT】
架构层：
- L3 不演化：EvalAdapter 是可插拔库，新增/替换由人工 + L2 触发，避免无限递归到 L4。
- L3 不进 L1 graph；独立于 L1 主执行链。
- L3 复用 L2 daemon thread pattern：独立 thread，不嵌套。
- L3 默认 enabled=false：数据驱动触发后才启用。

契约层：
- L2 调 L3 的唯一入口是 OrchestrationBridge.evaluate(ctx) → EvalResult。
- L3 自建 EvalBridge Protocol：不共享 skill 的 EvalBridge
  （skill 用 SkillRecord 专属类型，不能跨模块共享）。
- L3 evaluate 是同步阻塞调用（与 L1 sync only 一致）。
- fail-closed：evaluate 失败返回 EvalResult(success=False)，不抛异常；
  L2 收到 success=False → reject candidate + 保持旧 is_active。

评估器层：
- 3 个 adapter：programmatic / llm_judge / longitudinal_pairs；
  选择由 Bridge 自动（_select_method）。
- task_sample 由 L2 抽样传入；L3 不重复实现 task 池管理。

健康监控层：
- SpecialistRuntimeTracker 自建：pattern 复用 skill RuntimeTracker 趋势算法，
  但不实现 skill Protocol（返回类型 SkillHealthReport 字段不兼容）。
- degraded_specialists 命中 → enqueue L2 daemon thread queue；
  不直接调 L2 TriggerManager（解耦）。

决策日志层：
- 异步写：fire-and-forget，不阻塞 L1 turn。
- 不直接注入 prompt：作为 EvolutionMutator 输入样本（类似 L2 failure cases）。
- 保留 90 天 + 归档：移到 archive 表，不删除。

兼容与可观测性：
- L3 读 L1 metrics 复用 L2 MetricsView Protocol（扩展加 get_specialist_l2_events）。
- L3 启用时 L2 改调 OrchestrationBridge，L1 ResultSummarizer 不动（向后兼容）。
- L3 metrics 复用 l2_metrics 表（event_type 加 l3_ 前缀，不新建 metrics 表）。
- L3 CLI 设计保留暂不实现：命令方式交互复杂不便观测，等待更好可观测形态。
- 复用 L2 自建类型：EvalTask / EvalResult / _wilson_ci（不重复定义）。

启用条件（数据驱动，任一满足）：
- L2 演化产物 > 5 版 + floor eval 不足；
- 多 specialist 对比需求；
- specialist completion_rate < 0.4 持续；
- 跨 session lessons 累积需求；
- 用户主动需求。
"""