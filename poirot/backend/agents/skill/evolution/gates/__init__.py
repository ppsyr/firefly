"""自进化门控（evolution gates）。

【整体职责】
定义 evolution 闭环的"门控"环节：决定 candidate 能否晋升，以及上线后退化时能否回滚。
2a 实现 ScoreDeltaGate + GitRatchet；其余门为 2b/L3 预留 Protocol。

【内容摘要】
- ScoreDeltaGate        ：晋升决策门（2a）。candidate score > baseline + 无 hard_failure → accept。
- GitRatchet            ：上线后兜底回滚（2a，非决策门）。effective_rate 跌破阈值 → rollback。
- ChampionGateProtocol  ：2b 预留，score delta + hard_failures 双门。
- HITLGateProtocol      ：2b 预留，人审装饰器，accept 转 pending_human。
- CompositeGateProtocol ：2b 预留，cascading 链式，早期 reject short-circuit。
- ValidationGateProtocol：L3 预留，held-out 重放 + longitudinal pairs。
- MultiJudgeGateProtocol：L3 预留，多 LLM judge + majority vote。

【职责边界】
- 只负责：晋升决策（ScoreDeltaGate）+ 上线后回滚监控（GitRatchet）。
- 不负责：评估（eval_bridge）、变异（mutator）、聚焦（focuser）、触发（trigger）、
  持久化（store，仅调 rollback / get_versions）、编排（EvolutionManager）。
- 不做 LLM 自评：决策基于 EvalResult（D7）。
- 不删历史：rollback 只切 is_active 指针，旧 node 保留（version DAG）。

【INVARIANT】
- 所有门实现 PromotionGate Protocol 的 decide(candidate, baseline, eval_result)
  -> GateDecision。
- ScoreDeltaGate：hard_failures 非空 → reject；CAPTURED 无 baseline → score>0 即 accept；
  FIX/DERIVED → score > baseline_score + min_delta。
- GitRatchet：anti-loop（selections < min 不评判）；effective_rate >= threshold 不 rollback；
  回滚目标优先 parent_skill_ids，兜底 generation 最低。
- 2a 实现 ScoreDeltaGate + GitRatchet；ChampionGate / HITLGate / CompositeGate /
  ValidationGate / MultiJudgeGate 的 Protocol 留 2b / L3。
"""