"""Multi-Agent L2 Evolution Layer — 演化 ContextSummaryTemplate / SkillInjectionTemplate。

【整体职责】
L2 是 multiagent 的"进化层"：读 L1 指标与失败分类，用 LLM 变异出新的 per-call 模板
（ContextSummaryTemplate / SkillInjectionTemplate），经评估与晋升门决定是否采纳，
并持久化为可版本化 / 可回滚的演化产物。

【核心立场】
L2 不演化 Router（Router = LLM，不存在独立 Router 组件）。L2 只演化"LLM 能看到
但不进 system prompt cache prefix 的 per-call 产物"——这类产物 hot swap 不破坏
prompt caching。

【内容摘要】
- 触发：TriggerManager + L2TriggerMiddleware（四源 + 1h 冷却）。
- 聚焦：FailureFocuser（按 failure_category 聚类取 top，不调 LLM）。
- 变异：EvolutionMutator（单次 LLM + 结构化 JSON + 重试）。
- 评估与晋升：PromotionGate（longitudinal pairs + Wilson CI + hash 防环）。
- 持久化：VersionDAG（is_active 单指针 + 回滚 + hash 防环）。
- 预算：BudgetGuard（三维度记账 + 超限 fallback lead）。
- 指标：OrchestrationMetricsL2（11 种事件）。
- 消费：L2EvolutionWorker（daemon 线程 + per-profile 串行）。
- 上下文增强：IntentEngineStrengthened（给 ContextSummarizer 供 candidate metadata）。

【职责边界】
- 只负责：演化 per-call 模板、评估晋升、持久化版本、预算记账、指标打点。
- 不负责：specialist 执行（runtime 负责）、路由决策（LLM 负责）、
  L1 指标打点（L1 负责）、L3 内部评估实现（L3 负责）。
- 不进 L1 graph：L2 通过 L2TriggerMiddleware 钩子接入，不修改 L1 主执行链。

【INVARIANT】（40 条，按主题分组）

核心架构：
- L2 不直接读 OrchestrationStore，只通过 MetricsView Protocol。
- L2 不演化 Router（Router 不存在，由 LLM 决策）。
- L2 演化产物仅限 ContextSummaryTemplate + SkillInjectionTemplate（W2 + W4）。
- L2TriggerMiddleware 不调 LLM，不修改 ThreadState。
- L2EvolutionWorker per-profile 串行，不并发演化。
- 演化产物 hot swap 不破坏 prompt caching（因不进 cache prefix）。
- PromotionGate 用 95% CI 决策 + hash 防环，拒绝单次偶然。
- L2 不引入自动 retry（retry 仍由 LLM 决策）。
- 演化产物形态为结构化 dataclass，非字符串模板。
- BudgetGuard 超限 fallback 到 lead 自己做，不 fallback 另一 specialist。

持久化与读取：
- VersionDAG 持久化用 SQLite（multiagent.db 加表，与 L1 同 db 不同表）。
- L1 每次 specialist 调用查 DB 取 is_active，不缓存（保 hot swap）。
- 演化失败时 is_active 不变（演化失败 = 不演化）。

EvolutionMutator：
- 单次 LLM 调用，最多重试 2 次（含首次共 3 次）。
- 演化失败保持旧 is_active，不阻塞后续 L2 任务。
- 连续 3 次演化失败 → 标记 failure pattern "evolution_blocked"，需人工 inspect。
- 演化输入样本数 ≤ 5，按 failure_category 聚类取 top。
- 默认用 lead 同 model，可配置覆盖。

PromotionGate eval：
- eval 样本数 10-15，混合 80% 失败 task + 20% 成功 task。
- CI 用 Wilson score interval（z=1.96），不用正态近似。
- 单个 task 累计被 eval 用 ≤ 3 次，超过从 pool 淘汰。
- eval 整体超时 30 min → 中断 + 保持旧 is_active。
- 连续 3 次 eval 失败 → 标 "eval_blocked"，需人工 inspect。
- candidate 95% CI 下界 > baseline 95% CI 上界 → accept；否则 reject。

触发与节流：
- cron 周期默认 6h，冷却默认 1h，均可配置。
- 失败聚焦触发窗口 24h + 阈值 5 次；specialist 降级阈值 invoked ≥ 5 + rate < 0.4。
- anti-loop hash 窗口默认 5 版。
- Worker 走 daemon thread 单 worker 串行，不加额外锁。
- evolution_blocked / eval_blocked 24h 自动解除 + CLI 手动解除。

BudgetGuard：
- 三维度记账（token / cost_usd / 调用次数），cost_usd 为主触发维度。
- budget per-day UTC 0 点重置，跨 session 持久化于 multiagent.db。
- budget 超限 fallback 到 lead，通过 tool 返回 JSON 通知 LLM（不污染 system prompt）。
- budget 80% 预警写 metrics，不主动注入 system prompt。

IntentEngine：
- IntentEngineStrengthened 不作为 before_model middleware，不注入 system prompt。
- candidate metadata 通过 ContextSummarizer 渲染进 context_summary（per-call 产物）。
- IntentTree 始终启用 + 零 LLM 成本；LLM 兜底仅数据触发后启用。
- LLM 兜底用 lead 同 model，失败 fallback 到 IntentTree。

可观测性：
- L2 metrics 写 multiagent.db l2_metrics 表，不进 ThreadState。
- L2 告警不主动 push，用户通过 CLI 主动 inspect。
- L2 CLI 命令树 poirot multiagent l2 <verb>，设计保留暂不实现。
"""