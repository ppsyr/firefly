"""Multi-Agent Orchestration 系统 — L1 基础编排层 + L2 智能编排层。

【整体职责】
把 specialist 作为 tool 接入 lead agent 的 ReAct graph，通过 soft routing 让
lead agent 自主决定"委派给谁"或"自己干"。
L1 负责基础编排（派活 / 执行 / 回传），
L2 负责智能编排（失败演化 / 版本晋升 / 预算控制）。

【内容摘要】
- L1 基础编排层：specialist tool + soft routing + 4 个 specialist + 4 个 runtime。
- L2 智能编排层：失败聚焦 → 模板演化 → 评估 → 晋升。
- L3 评估层：为 L2 晋升提供评估能力（默认关闭）。

【职责边界】
- 只负责：specialist 编排、演化、评估。
- 不负责：Lead agent 决策（Leader 负责）、沙箱执行（sandbox 层负责）、
  技能管理（skill 层负责）。

【L1 INVARIANT】
1. specialist 黑盒——不管理其内部 context，只传 goal + context_summary + sandbox_id。
2. specialist 自带 model——只发现凭证，不为其配置 model。
3. shared thread sandbox——lead + self-copy + specialist 同一 sandbox_id。
4. leaf role 递归控制——子 agent tool_groups 不含 multiagent，不能 spawn。
5. sync only——不做异步。
6. pairing 完整性——specialist 失败抛 SpecialistError，转 error ToolMessage。
7. 消息角色交替——specialist 调用作为 tool result 回流，不插入 synthetic user message。
8. 凭证不进 LLM 主态——凭证只传 runtime，不写 ThreadState。
9. 8 接口抽象——specialist 经 SpecialistMcpServer 调 Poirot 8 个沙箱接口。
10. programmatic eval floor——success_criteria 强制，ResultSummarizer 内校验。

【L2 INVARIANT（摘要）】
- 不演化 Router（Router 就是 LLM）。
- 演化产物仅 W2（ContextSummaryTemplate）+ W4（SkillInjectionTemplate）。
- hot swap 不破 cache（per-call 产物，不进 cache prefix）。
- L2TriggerMiddleware 不调 LLM、不改 ThreadState。
- per-profile 串行（daemon thread 单 worker）。
- Wilson 95% CI + hash 防环 5 版。
- BudgetGuard 超限 fallback lead。
- VersionDAG SQLite + is_active 单指针。
- 演化失败保持旧 is_active。
- IntentEngine 不进 middleware（作为 ContextSummarizer 输入源）。
- L2 CLI 暂不实现。
"""