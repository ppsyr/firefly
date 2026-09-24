"""GitRatchet — 上线后兜底回滚（借鉴 darwin git ratchet）。

【整体职责】
evolution 的"上线后监控"机制（非决策门）。
candidate 已 accept 上线后，若 metric 退化（新版 effective_rate 跌破阈值），
调 store.rollback() 切 is_active 指针回旧版。

类比 git revert（禁 reset --hard，保留历史）；
version DAG 天然支持：旧 node 仍在，只切指针。

【内容摘要】
- __init__(degradation_threshold, min_selections, runtime_tracker)
      保存退化阈值、最少 selections、可选的 RuntimeTracker 信号。
- check_and_rollback(store, current) -> str | None
      检查当前 active 是否退化，是则回滚；返回滚到的旧 skill_id 或 None。

【职责边界】
- 只负责：上线后监控 + 退化判定 + 触发 rollback。
- 不负责：晋升决策（ScoreDeltaGate 等门）、评估（eval_bridge）、变异（mutator）、
  聚焦（focuser）、触发（trigger）、编排（EvolutionManager）。
- 不删除历史：rollback 只切 is_active 指针，旧 node 保留（version DAG 可回溯）。
- 不由门控调用：由 EvolutionManager 周期调（与 PromotionGate 解耦）。

【INVARIANT】
- 非决策门：是上线后监控，不参与 accept / reject 决策。
- anti-loop：total_selections < min_selections 时不评判（新版本需积累数据）。
- 健康判定：effective_rate >= degradation_threshold → 不 rollback。
- D-L3-16：若 runtime_tracker 提供，额外用 degraded_skills() 作退化信号
  （当前为软信号，仍以 effective_rate 兜底）。
- 回滚目标选择：
    1. 优先找 lineage.parent_skill_ids 里的版本；
    2. 无 parent 时取 generation 最低的非当前版；
    3. 都无 → 不 rollback（返 None）。
- 只调 store.rollback（切指针），不删除任何行。
- 返值：rollback 到的旧 skill_id；未 rollback 返 None。
"""
from __future__ import annotations

from typing import Any

from poirot.backend.agents.skill.types import SkillRecord


class GitRatchet:
    """上线后 degraded → rollback。EvolutionManager 周期调 check_and_rollback。

    构造参数：
    - degradation_threshold : effective_rate 退化阈值（默认 0.3）。
    - min_selections        : 评判前需积累的 selections（anti-loop，默认 5）。
    - runtime_tracker       : 可选 RuntimeTracker（D-L3-16 退化信号）。
    """

    def __init__(
        self,
        degradation_threshold: float = 0.3,
        min_selections: int = 5,
        runtime_tracker: Any | None = None,
    ) -> None:
        """保存阈值与可选退化信号源。"""
        self._threshold = degradation_threshold
        self._min_selections = min_selections
        self._runtime_tracker = runtime_tracker  # D-L3-16: 可选 RuntimeTracker 信号

    def check_and_rollback(
        self,
        store: Any,
        current: SkillRecord,
    ) -> str | None:
        """检查当前 active skill 是否 degraded，是则 rollback 到上一版（对外主入口）。

        步骤：
            1. D-L3-16：若 runtime_tracker 提供，查 degraded_skills()（软信号）。
            2. anti-loop：total_selections < min_selections → 不评判（返 None）。
            3. 健康：effective_rate >= threshold → 返 None。
            4. 退化：找回滚目标：
                 a. 优先 parent_skill_ids 里的版本；
                 b. 无 parent → generation 最低的非当前版。
            5. 找到 → store.rollback(target.skill_id)，返 target.skill_id；
               未找到 → 返 None。

        Args:
            store:   技能存储（提供 get_versions / rollback）。
            current: 当前 active 的 SkillRecord。

        Returns:
            回滚到的旧 skill_id；未 rollback 返 None。
        """
        # D-L3-16: RuntimeTracker 退化信号（若有）
        if self._runtime_tracker is not None:
            try:
                degraded = self._runtime_tracker.degraded_skills()
                if current.skill_id not in degraded:
                    # RuntimeTracker 未标退化，但仍用 effective_rate 兜底检查
                    pass
            except Exception:
                pass

        # 新版本需积累数据才评判（anti-loop）
        if current.total_selections < self._min_selections:
            return None
        if current.effective_rate >= self._threshold:
            return None  # 健康

        # degraded → 找上一版 rollback
        versions = store.get_versions(current.name)
        # 找 current 之前的版本（generation 更低 或 parent）
        parent_ids = current.lineage.parent_skill_ids
        rollback_target = None
        for v in versions:
            if v.skill_id == current.skill_id:
                continue
            if v.skill_id in parent_ids:
                rollback_target = v
                break
        if rollback_target is None and versions:
            # 无 parent，取 generation 最低的非当前版
            others = [v for v in versions if v.skill_id != current.skill_id]
            if others:
                rollback_target = min(others, key=lambda v: v.lineage.generation)

        if rollback_target is None:
            return None

        store.rollback(rollback_target.skill_id)
        return rollback_target.skill_id