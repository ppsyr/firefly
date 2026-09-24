"""BudgetTrackerExecutor — 预算追踪器。

【整体职责】
管理 governance["default"]["budget"] 这块状态：
    - init_budget      ：agent 启动时初始化结构
    - track            ：每次 after_model 累计 token、算 current/fraction、标 pending
    - clear_run_state  ：agent 结束时清掉本次 run 的状态字段

【budget 结构】
    {"input": 累计输入 token,
     "output": 累计输出 token,
     "total": input + output,
     "current": 当前 messages 的 token 数（token_counter 算），
     "window": 模型上下文窗口,
     "fraction": current / window}

【seen_msgs】
    {message_id: (last_input_tokens, last_output_tokens)}
    用于对同一条 AIMessage 的 usage_metadata 做增量累计（只加 diff，不重复加）。

【pending 判定】
    按 thresholds 依次比对 fraction，命中即 append 阶段名：
        P1（p1_externalize）→ P2（p2_thinking）→ P4（p4_summarize）→ P5（p5_stop_toolcall）
    注意：P3 不在此处 append（strategy.before_model 里另有 P3 分支依赖 fraction 阈值，
    但 pending 列表不含 P3——见 strategy.py 中 P3 分支实际由 pending 列表判断）。

【与 strategy.py 的关系】
    - before_agent 调 init_budget
    - after_agent  调 clear_run_state
    - after_model  调 track
    strategy.py 只读 budget.fraction / pending 决定走哪个压缩分支。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage


class BudgetTrackerExecutor:
    """累计 token + 算 fraction + 标 pending。"""

    def __init__(self, thresholds: dict[str, float]) -> None:
        """保存阈值表（来自 strategy.py 的 _DEFAULT_THRESHOLDS + params.thresholds 合并）。"""
        self._thresholds = thresholds

    def init_budget(self, governance: dict | None) -> dict:
        """Agent 启动时初始化预算与标志位。

        Args:
            governance: 现有 governance dict（可为 None）。

        Returns:
            新的 governance，default 下重置：
                budget = {input/output/total/current/window/fraction=0}
                seen_msgs = {}
                pending = []
                warned = False
                p1_completed = False
                p1_skip_until_fraction = 0.0
        """
        g = dict(governance or {})
        d = dict(g.get("default") or {})
        d["budget"] = {"input": 0, "output": 0, "total": 0, "current": 0, "window": 0, "fraction": 0.0}
        d["seen_msgs"] = {}
        d["pending"] = []
        d["warned"] = False
        d["p1_completed"] = False
        d["p1_skip_until_fraction"] = 0.0
        g["default"] = d
        return g

    def track(self, governance: dict | None, messages: list, token_counter: Any, window: int) -> dict:
        """累计 token、算 fraction、标 pending。

        Args:
            governance:    现有 governance。
            messages:      当前消息列表。
            token_counter: 可调用对象，算 messages 总 token。
            window:        模型上下文窗口大小。

        Returns:
            更新后的 governance（budget.fraction / pending / seen_msgs 已写回）。

        累计逻辑：
            - 遍历 AIMessage，读 usage_metadata.input_tokens/output_tokens；
              与 seen_msgs 里记录的上次值做 diff，只把增量加进 budget.input/output。
            - current = token_counter(messages)，fraction = current / window。
            - 按阈值填 pending。
        """
        g = dict(governance or {})
        d = dict(g.get("default") or {})
        seen = dict(d.get("seen_msgs") or {})
        budget = dict(d.get("budget") or {"input": 0, "output": 0, "total": 0, "current": 0, "window": window, "fraction": 0.0})
        budget["window"] = window

        # 增量累计 usage_metadata（同 message_id 只加 diff）
        for msg in messages:
            if isinstance(msg, AIMessage) and msg.id:
                usage = getattr(msg, "usage_metadata", None) or {}
                in_t = usage.get("input_tokens", 0)
                out_t = usage.get("output_tokens", 0)
                prev_in, prev_out = seen.get(msg.id, (0, 0))
                diff_in = max(0, in_t - prev_in)
                diff_out = max(0, out_t - prev_out)
                if diff_in or diff_out:
                    budget["input"] += diff_in
                    budget["output"] += diff_out
                    budget["total"] += diff_in + diff_out
                    seen[msg.id] = (in_t, out_t)

        # 当前 messages 的 token 数与占用比例
        current = token_counter(messages)
        budget["current"] = current
        budget["fraction"] = current / window if window > 0 else 0.0

        # 按阈值标 pending（P1/P2/P4/P5；P3 不在 pending 列表）
        fraction = budget["fraction"]
        pending: list[str] = []
        if fraction >= self._thresholds["p1_externalize"]:
            pending.append("P1")
        if fraction >= self._thresholds["p2_thinking"]:
            pending.append("P2")
        if fraction >= self._thresholds["p4_summarize"]:
            pending.append("P4")
        if fraction >= self._thresholds["p5_stop_toolcall"]:
            pending.append("P5")

        d["budget"] = budget
        d["seen_msgs"] = seen
        d["pending"] = pending
        g["default"] = d
        return g

    def clear_run_state(self, governance: dict | None) -> dict:
        """Agent 结束时清掉本次 run 的状态字段。

        Args:
            governance: 现有 governance。

        Returns:
            新的 governance，default 下已 pop：
                budget / seen_msgs / pending / warned /
                p1_completed / p1_skip_until_fraction
            保留 default 下其它字段（如 summary / snapshot_path / metrics）。
        """
        g = dict(governance or {})
        d = dict(g.get("default") or {})
        for k in ("budget", "seen_msgs", "pending", "warned", "p1_completed", "p1_skip_until_fraction"):
            d.pop(k, None)
        g["default"] = d
        return g