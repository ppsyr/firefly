"""自进化触发器（evolution triggers）。

【整体职责】
定义 evolution 闭环的第一环：决定"何时、对哪个 skill、发起哪类进化"。
触发器只产 EvolutionContext，不负责聚焦 / 变异 / 评估 / 门控。

【内容摘要】
- CaptureTrigger      ：CAPTURED 触发（新技能沉淀）。2a 仅手动 capture；
                        自动信号（重复成功模式 / agent 自评）留 2b。
- MetricMonitorTrigger：METRIC 触发（周期扫 metrics 诊断 FIX）。
                        两阶段筛选（规则 + LLM 确认）+ anti-loop
                        （min_selections / cooldown_turns）。

【职责边界】
- 只负责：扫信号 + 过滤 + 产 EvolutionContext。
- 不负责：聚焦 / 变异 / 评估 / 门控 / 持久化 / 编排（均在其他模块）。
- 触发器的调用方是 EvolutionManager：
    - 自动路径：run_cycle → should_trigger
    - 手动路径：capture_skill → manual_capture

【INVARIANT】
- 触发器实现 Trigger Protocol：should_trigger(store) -> list[EvolutionContext]。
- MetricMonitorTrigger 额外实现 mark_evolved(name, selections)，供 anti-loop 锚点更新。
- CaptureTrigger 额外实现 manual_capture(pattern, suggested_name)，供手动 CAPTURED。
- 2a 只产 FIX（MetricMonitor）+ 手动 CAPTURED（Capture）；DERIVED 与自动 CAPTURED 留 2b。
"""