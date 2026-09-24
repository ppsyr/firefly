"""DefaultStrategy — 默认上下文治理策略。

【整体职责】
实现 contract.GovernanceStrategy 的 6 个 hook，按 token 占用 fraction 分阶段
（P1/P2/P3/P4/P5）触发上下文压缩动作。策略内部把具体执行委托给 4 个 Executor：
    - BudgetTrackerExecutor ：算 token / fraction / pending 阶段
    - ExternalizerExecutor  ：外化超长 tool result 到磁盘
    - SnapshotExecutor      ：P4 压缩前存快照
    - SummarizerExecutor    ：P4 用 LLM 全量摘要旧消息

【注册】
@register_strategy("default") 装饰，供 registry.get_strategy_class("default") 查表。

【阈值（_DEFAULT_THRESHOLDS，可被 params.thresholds 覆盖）】
    p1_externalize   0.40  → P1 外化
    p2_thinking      0.50  → P2（标记 thinking，当前主要由 _mark_thinking 承担）
    p3_observations  0.60  → P3 设 p3_obs_limit（当前渲染层未消费，no-op）
    p4_summarize     0.80  → P4 摘要
    p5_stop_toolcall 0.90  → P5 剥 tool_calls + 收尾提示 + jump model
    hard_stop        0.99  → 硬停（P5 分支用文案区分）

【hook 编排概览】
    before_agent  → init_budget
    after_agent   → clear_run_state
    before_model  → pairing 补全 + P3/P4/P1 按 pending 顺序执行
    after_model   → track + _mark_thinking + P5 剥 tool_calls
    wrap_model_call  → no-op
    wrap_tool_call   → 单条 tool result 外化（externalize_if_needed）
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from poirot.backend.agents.context_engineering.contract import (
    GovernanceContext,
    GovernanceResult,
)
from poirot.backend.agents.context_engineering.registry import register_strategy
from poirot.backend.agents.context_engineering.strategies.default._constants import CST
from poirot.backend.agents.context_engineering.strategies.default.budget import (
    BudgetTrackerExecutor,
)
from poirot.backend.agents.context_engineering.strategies.default.externalizer import (
    ExternalizerExecutor,
)
from poirot.backend.agents.context_engineering.strategies.default.snapshot import (
    SnapshotExecutor,
)
from poirot.backend.agents.context_engineering.strategies.default.summarizer import (
    SummarizerExecutor,
)
from poirot.backend.agents.context_engineering.utilities import resolve_window_size
from poirot.backend.agents.middlewares.tagged_context_middleware import POIROT_THINKING

logger = logging.getLogger(__name__)

# 默认阈值表；实例化时与 params["thresholds"] 合并（后者覆盖前者）
_DEFAULT_THRESHOLDS: dict[str, float] = {
    "p1_externalize": 0.40,
    "p2_thinking": 0.50,
    "p3_observations": 0.60,
    "p4_summarize": 0.80,
    "p5_stop_toolcall": 0.90,
    "hard_stop": 0.99,
}


@register_strategy("default")
class DefaultStrategy:
    """默认上下文治理策略：分段优先级舍弃 P0-P5。"""

    def __init__(self, params: dict | None = None, model: Any = None, summarize_model: Any = None) -> None:
        """构造策略，实例化 4 个 Executor。

        Args:
            params:          策略参数，可含 thresholds / externalize_dir /
                             externalize_min_chars / externalize_preview_chars /
                             exempt_rounds / tool_metadata / preserve_recent /
                             snapshot_dir / summarize_model。
            model:           主模型对象（after_model 算 window 用）。
            summarize_model: 摘要专用模型；缺省回退到 model。
        """
        params = params or {}
        self._model = model
        self._summarize_model_name: str | None = params.get("summarize_model")
        # 阈值：默认 + params.thresholds 覆盖
        self._thresholds: dict[str, float] = {
            **_DEFAULT_THRESHOLDS,
            **(params.get("thresholds") or {}),
        }
        # 4 个 Executor
        self._budget = BudgetTrackerExecutor(self._thresholds)
        self._externalizer = ExternalizerExecutor(
            externalize_dir=params.get("externalize_dir", ".poirot/externalized"),
            min_chars=params.get("externalize_min_chars", 500),
            preview_chars=params.get("externalize_preview_chars", 500),
            exempt_rounds=params.get("exempt_rounds", 2),
            tool_metadata=params.get("tool_metadata"),
        )
        self._summarizer = SummarizerExecutor(
            model=summarize_model or model,
            preserve_recent=params.get("preserve_recent", 6),
        )
        self._snapshot = SnapshotExecutor(
            snapshot_dir=params.get("snapshot_dir", ".poirot/snapshots"),
        )

    # ---------- before_agent / after_agent：初始化与清理 ----------

    def before_agent(self, ctx: GovernanceContext) -> GovernanceResult:
        """Agent 启动：初始化 budget 结构（input/output/total/current/window/fraction + seen_msgs + pending + 标志位）。

        Returns:
            GovernanceResult(state_patch={"governance": ...})
        """
        governance = self._budget.init_budget(ctx.governance)
        return GovernanceResult(state_patch={"governance": governance})

    async def abefore_agent(self, ctx: GovernanceContext) -> GovernanceResult:
        """before_agent 的 async 版，直接复用同步实现。"""
        return self.before_agent(ctx)

    def after_agent(self, ctx: GovernanceContext) -> GovernanceResult:
        """Agent 结束：清掉本次 run 的预算/标志位字段（budget/seen_msgs/pending/warned/...）。

        Returns:
            GovernanceResult(state_patch={"governance": ...})
        """
        governance = self._budget.clear_run_state(ctx.governance)
        return GovernanceResult(state_patch={"governance": governance})

    async def aafter_agent(self, ctx: GovernanceContext) -> GovernanceResult:
        """after_agent 的 async 版，直接复用同步实现。"""
        return self.after_agent(ctx)

    # ---------- before_model：pairing 补全 + P3/P4/P1 压缩 ----------

    def before_model(self, ctx: GovernanceContext) -> GovernanceResult:
        """送 LLM 前的主压缩入口。

        执行顺序：
            1. pairing 补全（_ensure_pairing）：AIMessage(tool_calls) 缺配对 ToolMessage
               时补 error 占位；不早退，与 compaction 并行。
            2. P3（若 pending）：设 p3_obs_limit（当前渲染层未消费，no-op）。
            3. P4（若 pending）：snapshot → summarize，合并 pairing_patch 后返回。
            4. P1（若 pending）：externalize_history，带间隔抑制（p1_skip_until_fraction）。
            5. 无 compaction 时，仅返 pairing_patch / P3 governance 更新。

        Returns:
            GovernanceResult，可能含 state_patch / messages_patch / jump_to。
        """
        # 防御性配对校验：送 LLM 前确保 AIMessage(tool_calls) 都有配对 ToolMessage。
        # 不早退——pairing 补全与 compaction（P1/P4）并行执行，避免 pairing 问题阻塞压缩。
        pairing_patch = self._ensure_pairing(ctx.messages or [])
        if pairing_patch:
            logger.warning("pairing 补全：%d 个缺失 ToolMessage", len(pairing_patch))

        governance = ctx.governance or {}
        d = governance.get("default") or {}
        pending = d.get("pending") or []
        fraction = (d.get("budget") or {}).get("fraction", 0.0)

        # P3: observations top-N（渲染层筛）。
        # 当前 observations 不进上下文块（Q5 舍弃 <observations> 标签），
        # P3 设 p3_obs_limit flag 供未来渲染层消费，当前为 no-op。
        if "P3" in pending:
            obs_count = len(ctx.state.get("observations") or [])
            p3_limit = max(3, obs_count // 3) if fraction >= 0.60 else obs_count
            d["p3_obs_limit"] = p3_limit
            governance = dict(governance)
            governance["default"] = d
            logger.info("compaction P3 observations top-N=%d (total=%d, fraction=%.2f)", p3_limit, obs_count, fraction)
            self._emit_trace(ctx, "triggered", "P3", obs_limit=p3_limit, obs_total=obs_count, fraction=fraction)

        if "P4" in pending:
            # P4：先存快照，再全量摘要；摘要结果与 pairing_patch 合并
            logger.info("compaction P4 summarize 触发 fraction=%.2f", fraction)
            self._emit_trace(ctx, "triggered", "P4", fraction=fraction, window=(d.get("budget") or {}).get("window", 0), pending=pending)
            snapshot_gov = self._snapshot.snapshot_if_pending(ctx.governance, ctx.messages or [], ctx.state)
            effective_gov = snapshot_gov or ctx.governance
            result = self._summarizer.summarize_if_pending(effective_gov, ctx.messages or [], self._externalizer)
            self._emit_trace(ctx, "completed", "P4", fraction_after=fraction)
            if result is not None:
                # 合并 pairing_patch 进 compaction result
                if pairing_patch:
                    combined_msgs = (result.messages_patch or []) + pairing_patch
                    return GovernanceResult(
                        state_patch=result.state_patch,
                        messages_patch=combined_msgs,
                        jump_to=result.jump_to,
                    )
                return result

        if "P1" in pending:
            # 间隔抑制：fraction 未涨超 p1_skip_until_fraction → 跳过
            skip_until = d.get("p1_skip_until_fraction", 0.0)
            if fraction < skip_until:
                logger.info("compaction P1 skipped (interval suppression): fraction=%.2f < skip_until=%.2f", fraction, skip_until)
                self._emit_trace(ctx, "skipped", "P1", reason="interval_suppression", fraction=fraction)
            else:
                logger.info("compaction P1 externalize 触发 fraction=%.2f", fraction)
                self._emit_trace(ctx, "triggered", "P1", fraction=fraction, pending=pending)
                messages_patch = self._externalizer.externalize_history(ctx.messages or [])
                ext_count = len(messages_patch) if messages_patch else 0
                self._emit_trace(ctx, "completed", "P1", externalized_count=ext_count, fraction_after=fraction)
                # 更新间隔抑制 flag
                ext_count = len(messages_patch) if messages_patch else 0
                new_skip = fraction + 0.10
                d["p1_skip_until_fraction"] = new_skip
                d["p1_completed"] = (ext_count == 0)
                governance["default"] = d
                if messages_patch:
                    if pairing_patch:
                        messages_patch = messages_patch + pairing_patch
                    return GovernanceResult(
                        state_patch={"governance": governance},
                        messages_patch=messages_patch,
                    )
                # P1 执行但无可外化内容 → 更新 governance 后继续检查 pairing_patch
                logger.info("compaction P1 completed (no externalizable content, p1_completed=True)")
                if pairing_patch:
                    return GovernanceResult(
                        state_patch={"governance": governance},
                        messages_patch=pairing_patch,
                    )
                return GovernanceResult(state_patch={"governance": governance})

        # 无 compaction 触发，仅返 pairing 补全 + P3 governance 更新（若有）
        if pairing_patch:
            return GovernanceResult(
                state_patch={"governance": governance} if "P3" in pending else None,
                messages_patch=pairing_patch,
            )
        if "P3" in pending:
            return GovernanceResult(state_patch={"governance": governance})
        return GovernanceResult()

    @staticmethod
    def _ensure_pairing(messages: list) -> list | None:
        """校验 AIMessage(tool_calls) 都有配对 ToolMessage，缺则补 error ToolMessage。

        Args:
            messages: 当前完整消息列表。

        Returns:
            缺失 tool result 的占位 ToolMessage 列表；无缺失返回 None。
        """
        tool_msg_ids = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
        patch = []
        for msg in messages:
            if isinstance(msg, AIMessage):
                for tc in msg.tool_calls or []:
                    tc_id = tc.get("id") if isinstance(tc, dict) else None
                    if tc_id and tc_id not in tool_msg_ids:
                        patch.append(ToolMessage(
                            content=f"⚠️ 工具结果缺失（{tc_id}），已补占位。",
                            tool_call_id=tc_id,
                            status="error",
                        ))
        return patch or None

    # ---------- trace / 事件发射 ----------

    @staticmethod
    def _emit_compaction_event(event_type: str, **fields: Any) -> None:
        """发 compaction custom event 到 stream。无 graph context 时 fallback logger。

        Args:
            event_type: 事件类型（如 compaction_start / compaction_end）。
            **fields:   附加字段，透传给 stream writer。
        """
        try:
            from langgraph.config import get_stream_writer

            get_stream_writer()({"type": event_type, **fields})
        except Exception:
            logger.info("compaction event (no stream writer): %s %s", event_type, fields)

    def _emit_trace(self, ctx: GovernanceContext, event: str, stage: str, **fields: Any) -> None:
        """compaction 全流程 trace：写 compaction.jsonl + journal 关键事件 + stream 事件。

        Args:
            ctx:    GovernanceContext（取 runtime 定位 run 目录）。
            event:  事件类型（triggered / completed / skipped）。
            stage:  阶段名（P1/P3/P4/P5）。
            **fields: 附加字段。
        """
        runtime = ctx.runtime
        ts = datetime.now(CST).isoformat()
        record = {"ts": ts, "event": event, "stage": stage, **fields}

        # 1. 写 compaction.jsonl
        run_dir = self._get_run_dir(runtime)
        if run_dir:
            try:
                os.makedirs(run_dir, exist_ok=True)
                with open(os.path.join(run_dir, "compaction.jsonl"), "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            except (OSError, TypeError) as exc:
                logger.warning("compaction trace write failed: %s", exc)

        # 2. 关键事件入 journal（events.jsonl）
        if event in ("triggered", "completed", "skipped"):
            try:
                from poirot.backend.agents.middlewares.run_journal_middleware import _get_runtime_value
                journal = _get_runtime_value(runtime, "journal", None)
                if journal is not None:
                    journal.append(f"compaction.{event}", {"stage": stage, **fields})
            except Exception:
                pass

        # 3. stream 事件（CLI 渲染 compaction_start/end）
        stream_type = {"triggered": "compaction_start", "completed": "compaction_end"}.get(event, f"compaction_{event}")
        self._emit_compaction_event(stream_type, tool_name=stage, **fields)

    @staticmethod
    def _get_run_dir(runtime: Any) -> str | None:
        """从 runtime 取 run 日志目录路径。

        Args:
            runtime: LangGraph runtime。

        Returns:
            ``.poirot/logs/threads/<tid>/runs/<rid>``；tid/rid 缺失时返回 None。
        """
        try:
            from poirot.backend.agents.middlewares.run_journal_middleware import _get_runtime_value
            tid = _get_runtime_value(runtime, "thread_id", None)
            rid = _get_runtime_value(runtime, "run_id", None)
            if tid and rid:
                return os.path.join(".poirot", "logs", "threads", str(tid), "runs", str(rid))
        except Exception:
            pass
        return None

    async def abefore_model(self, ctx: GovernanceContext) -> GovernanceResult:
        """before_model 的 async 版，直接复用同步实现。"""
        return self.before_model(ctx)

    # ---------- after_model：track + mark thinking + P5 ----------

    def after_model(self, ctx: GovernanceContext) -> GovernanceResult:
        """模型返回后：累计 token → 算 fraction → 标记 thinking → 触发 P5。

        执行顺序：
            1. 解析 window（config.window 优先，否则 resolve_window_size(model)）。
            2. BudgetTrackerExecutor.track：累计 usage + 算 current/fraction + 标 pending。
            3. _mark_thinking：给带 reasoning_content 的 AIMessage 打 POIROT_THINKING 标记。
            4. P5 分支：fraction >= 0.90 且未 warned → 剥最后一条 AI 的 tool_calls +
               插入 context_budget_stop HumanMessage + jump_to="model"。
               warned 标志防重复触发死循环。

        Returns:
            GovernanceResult（含 state_patch.governance，P5 时含 messages_patch + jump_to）。
        """
        messages = ctx.messages or []
        config = ctx.config or {}
        # window 优先 config 显式配置，无则 resolve_window_size(model) 查表（model 属性 → model_name 映射 → 128000）
        config_window = config.get("window") if isinstance(config, dict) else None
        window = config_window or resolve_window_size(self._model)
        governance = self._budget.track(ctx.governance, messages, ctx.token_counter, window)
        messages_patch = self._mark_thinking(messages)

        # P5/P99：fraction >= 90% → 剥 tool_calls + 收尾提示 + jump model（warned 防重复触发死循环）
        d = governance.get("default") or {}
        pending = d.get("pending") or []
        warned = d.get("warned", False)
        fraction = (d.get("budget") or {}).get("fraction", 0.0)

        if "P5" in pending and not warned:
            last_ai = next(
                (m for m in reversed(messages)
                 if isinstance(m, AIMessage) and getattr(m, "tool_calls", None)),
                None,
            )
            if last_ai:
                cleared = self._strip_tool_calls(last_ai, fraction)
                stop_msg = HumanMessage(
                    name="context_budget_stop",
                    additional_kwargs={"hide_from_ui": True},
                    content=self._build_stop_message(fraction),
                )
                d["warned"] = True
                governance["default"] = d
                combined_patch = (messages_patch or []) + [cleared, stop_msg]
                self._emit_trace(ctx, "triggered", "P5", fraction=fraction, action="strip_tool_calls")
                return GovernanceResult(
                    state_patch={"governance": governance},
                    messages_patch=combined_patch,
                    jump_to="model",
                )

        return GovernanceResult(state_patch={"governance": governance}, messages_patch=messages_patch)

    @staticmethod
    def _strip_tool_calls(last_ai: AIMessage, fraction: float) -> AIMessage:
        """剥 tool_calls 保 pairing：复用原 id → add_messages 替换（非追加）。

        同步剥 additional_kwargs.tool_calls/function_call 防 _has_tool_call_intent 误判。
        原始 AIMessage(tool_calls) 被 id 匹配替换 → jump model 跳过 ToolNode → 无孤立 tool_calls →
        下一轮 model 调用不 400。

        Args:
            last_ai:  最后一条带 tool_calls 的 AIMessage。
            fraction: 当前 token 占用比例（写入 additional_kwargs.context_budget_stop）。

        Returns:
            替换版 AIMessage（tool_calls=[]，附加 context_budget_stop 标记）。
        """
        new_kwargs = {
            k: v for k, v in (last_ai.additional_kwargs or {}).items()
            if k not in ("tool_calls", "function_call")
        }
        new_kwargs["context_budget_stop"] = round(fraction, 4)
        return AIMessage(
            id=last_ai.id,
            content=last_ai.content or "",
            tool_calls=[],
            additional_kwargs=new_kwargs,
        )

    @staticmethod
    def _build_stop_message(fraction: float) -> str:
        """构造收尾提示文案。

        Args:
            fraction: 当前 token 占用比例。

        Returns:
            fraction >= 0.99 用硬停文案；否则用 90% 收尾文案。
        """
        if fraction >= 0.99:
            return (
                "<system_reminder>\n"
                "上下文窗口占用已达 99% 硬底线，强制收尾。"
                "不可再调用任何工具，请立即基于已有信息给出最终答案。\n"
                "</system_reminder>"
            )
        return (
            "<system_reminder>\n"
            f"上下文窗口占用已达 {fraction:.0%}，超过 90% 阈值。"
            "请基于已有信息收尾，不再调用工具。若证据不足，基于现有信息给出最佳答案并说明缺口。\n"
            "</system_reminder>"
        )

    @staticmethod
    def _mark_thinking(messages: list) -> list | None:
        """给带 reasoning_content 且无 tool_calls 的 AIMessage 打 POIROT_THINKING 标记。

        Args:
            messages: 当前消息列表。

        Returns:
            需要替换的 AIMessage 列表；无匹配返回 None。
        """
        patch = []
        for msg in messages:
            if (isinstance(msg, AIMessage)
                    and msg.additional_kwargs.get("reasoning_content")
                    and not msg.additional_kwargs.get(POIROT_THINKING)
                    and not msg.tool_calls):
                new_kwargs = {**msg.additional_kwargs, POIROT_THINKING: True}
                patch.append(msg.model_copy(update={"additional_kwargs": new_kwargs}))
        return patch or None

    async def aafter_model(self, ctx: GovernanceContext) -> GovernanceResult:
        """after_model 的 async 版，直接复用同步实现。"""
        return self.after_model(ctx)

    # ---------- wrap hooks ----------

    def wrap_model_call(self, ctx: GovernanceContext) -> GovernanceResult:
        """wrap_model_call：no-op，不改 request。

        Returns:
            空 GovernanceResult（request_override=None，adapter 用原 request）。
        """
        return GovernanceResult()

    async def awrap_model_call(self, ctx: GovernanceContext) -> GovernanceResult:
        """wrap_model_call 的 async 版，no-op。"""
        return GovernanceResult()

    def wrap_tool_call(self, ctx: GovernanceContext) -> GovernanceResult:
        """工具调用后：对单条 ToolMessage 尝试外化。

        若 externalize_if_needed 返回替换版，则通过 request_override 输出；
        否则返回空 result（adapter 用原 tool_result）。

        Args:
            ctx: GovernanceContext（tool_result 为 ToolMessage）。

        Returns:
            GovernanceResult(request_override=替换版 ToolMessage 或 None)。
        """
        tool_result = ctx.tool_result
        if isinstance(tool_result, ToolMessage):
            rewritten = self._externalizer.externalize_if_needed(tool_result)
            if rewritten is not None:
                return GovernanceResult(request_override=rewritten)
        return GovernanceResult()

    async def awrap_tool_call(self, ctx: GovernanceContext) -> GovernanceResult:
        """wrap_tool_call 的 async 版，直接复用同步实现。"""
        return self.wrap_tool_call(ctx)