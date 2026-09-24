"""Skill 自进化层（Layer 2a 核心闭环）。

【整体职责】
定义 evolution（L2 进化层）的门面与公共 API。
它把"触发 → 聚焦 → 变异 → 评估 → 门控 → 晋升/记录"串成闭环，
消费 L1 的计数器与 L3 的评分，产出新版本写回 version DAG。

2a 能力边界：
- 支持 FIX（自动 METRIC + 手动 /skill evolve）。
- 支持 CAPTURED（仅手动 /skill capture）。
- DERIVED 与自动 CAPTURED 留 2b。

【内容摘要】
- EvolutionManager      : 闭环编排门面（run_cycle / evolve_skill / capture_skill）。
- Trigger               : 触发接口（should_trigger）。
- FailureFocuser        : 聚焦接口（focus）。
- Mutator               : 变异接口（mutate）。
- EvalBridge            : 评估转接接口（evaluate）。
- PromotionGate         : 门控接口（decide）。

子模块：
- types.py     : 值对象（EvolutionContext / EvalResult / GateDecision / EvolutionRecord 等）。
- protocols.py : 5 个 Protocol（Trigger / FailureFocuser / Mutator / EvalBridge / PromotionGate）。
- manager.py   : EvolutionManager。
- triggers/    : CaptureTrigger（手动 CAPTURED）/ MetricMonitorTrigger（自动 FIX）。
- focus/       : IVEFocuser（IVE 5 问诊断）。
- mutators/    : LLMMutator（FIX 编辑 + CAPTURED 生成）。
- gates/       : ScoreDeltaGate（晋升决策）/ GitRatchet（上线后回滚）。
- eval/        : ProgrammaticEvalBridge（L3 关闭时的兼容评估桥）。

【职责边界】
- 只负责：进化闭环的编排与执行（触发 / 聚焦 / 变异 / 门控 / 晋升 / 回滚）。
- 不负责：评估逻辑（归 eval/L3）、技能解析（parser）、选择（selector）、
  注入（injector）、打点触发（middleware）。
- 不做 LLM 自评决策：门控基于 EvalResult（D7）。
- 不删历史：create_version / rollback 只改 is_active 指针，version DAG 保留。
- 回滚独立于闭环：GitRatchet 是上线后监控，不在 _run_evolution 内。

【INVARIANT】
- 闭环七步固定：focus → mutate → eval → gate → promote → record → journal。
- 所有环节实现对应 Protocol；EvolutionManager 只调接口，不感知实现（零侵入）。
- 由 bootstrap 构造后经 SkillManager.set_evolution_manager 回注（避免循环依赖）。
- 2a 只支持 FIX + 手动 CAPTURED；DERIVED 与自动 CAPTURED 留 2b。
- 三处 anti-loop：trigger（min_selections + cooldown）、focuser（impl 累计升级）、
  ratchet（selections 门槛）。
- 与 eval 的解耦点：EvalBridge Protocol；共享数据契约定义在 evolution/types.py。
"""