"""StallDetectionMiddleware — 检测 agent 卡死并暂停求助。

【整体职责】
在 agent 反复失败、陷入死胡同时检测到「卡死（stall）」，并暂停 graph
向用户求助；如果求助次数用尽，则强制收尾。

【两个 hook 的分工】
- wrap_tool_call：记录工具失败（含异常路径），必要时置 pending_stuck 标记。
  **不在这里暂停**——因为并行 tool_calls 时暂停会打断 ToolMessage 配对，
  要等所有 ToolMessage 都就位后再暂停。
- after_model：记录 todo 状态；若 pending_stuck 为真，则在此执行暂停
  （返回 Command(goto=END)）。此时 ToolMessage 已全部就位，配对完整。

【暂停的实现】
after_model 返回 Command(goto=END)，把 graph 跳到终点，交回用户。
为了在 resume 时消息历史合法，暂停前会把所有悬空 tool_call 补上占位
ToolMessage。DanglingToolCallMiddleware 会在 resume 时兜底修补剩余缺口。

【求助次数上限】
max_help_requests 用尽后，不再暂停求助，而是走 _force_finalize 强制收尾。
"""

from __future__ import annotations

from typing import Any, override

from langchain.agents.middleware.types import AgentMiddleware, hook_config
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.graph import END
from langgraph.runtime import Runtime
from langgraph.types import Command

from poirot.backend.agents.middlewares.run_journal_middleware import _get_runtime_value
from poirot.backend.agents.observability.interrupt_protection import (
    is_interrupt_protected,
)
from poirot.backend.agents.observability.stall_tracker import StallTracker
from poirot.backend.agents.state.types import ThreadState


class StallDetectionMiddleware(AgentMiddleware):
    """当 StallTracker 检测到死胡同时，暂停 graph。

    内部按 run_id 维护三张表：
    - _trackers：        run_id → StallTracker（记录失败 / todo 状态）；
    - _help_counts：     run_id → 已求助次数；
    - _pending_stuck：   run_id → 是否已标记「待暂停」。
    """

    state_schema = ThreadState  # type: ignore[assignment]

    def __init__(self, max_help_requests: int = 3) -> None:
        """初始化。

        Args:
            max_help_requests: 同一个 run 内最多求助几次；超过则强制收尾。
        """
        self._trackers: dict[str, StallTracker] = {}
        self._help_counts: dict[str, int] = {}
        self._pending_stuck: dict[str, bool] = {}
        self._max_help = max_help_requests

    def _get_tracker(self, runtime: Runtime) -> StallTracker:
        """取当前 run 的 StallTracker；不存在则创建并缓存。"""
        run_id = _get_runtime_value(runtime, "run_id", None) or "default"
        if run_id not in self._trackers:
            self._trackers[run_id] = StallTracker()
        return self._trackers[run_id]

    def _help_count(self, runtime: Runtime) -> int:
        """取当前 run 已求助次数（缺省 0）。"""
        run_id = _get_runtime_value(runtime, "run_id", None) or "default"
        return self._help_counts.get(run_id, 0)

    def _increment_help(self, runtime: Runtime) -> None:
        """当前 run 求助次数 +1。"""
        run_id = _get_runtime_value(runtime, "run_id", None) or "default"
        self._help_counts[run_id] = self._help_counts.get(run_id, 0) + 1

    def _check_and_flag_stuck(
        self, runtime: Runtime, tool_name: str, tool_input: Any, error: str,
    ) -> None:
        """记录一次工具失败，若判定卡死则置 pending_stuck 标记。不在此暂停 graph。

        处理流程：
        1. 取当前 run 的 tracker。
        2. tracker.record_tool_failure(tool_name, tool_input, error)。
        3. 若 tracker.stuck 为真，把当前 run 的 _pending_stuck 置 True，
           留给 after_model 统一处理暂停。

        Args:
            runtime:   LangGraph 运行时（取 run_id）。
            tool_name: 失败的工具名。
            tool_input: 工具入参。
            error:     错误信息文本。
        """
        tracker = self._get_tracker(runtime)
        tracker.record_tool_failure(tool_name, tool_input, error)
        if tracker.stuck:
            run_id = _get_runtime_value(runtime, "run_id", None) or "default"
            self._pending_stuck[run_id] = True

    def _check_stuck_and_pause(
        self, state: ThreadState, runtime: Runtime,
    ) -> Command | None:
        """检查是否卡死并决定暂停 / 强制收尾。

        处理流程：
        1. 取当前 run 的 tracker；若 tracker.stuck 为假 → 返回 None（不处理）。
        2. 若处于中断保护上下文（is_interrupt_protected()）→ 返回 None，
           不打扰正在进行的中断保护。
        3. 若已求助次数 >= max_help_requests → _force_finalize 强制收尾。
        4. 否则正常求助：
           - 求助计数 +1；
           - 取 journal / run_id / reason；
           - journal.append("help.requested", {run_id, reason, failures})；
           - tracker.reset() 重置卡死状态。
        5. 暂停前补齐悬空 tool_call：
           - 收集已应答的 tool_call_id 到 answered_ids；
           - 遍历 AIMessage.tool_calls，未应答的补一条占位 ToolMessage
             （content 标 "[Skipped — stall detected (reason)]"），
             并加入 answered_ids。
        6. messages_update = 占位 ToolMessage + 一条隐藏的 HumanMessage
           （"[STALL DETECTED] ... Pausing for user help."，
           name="stall_detection"，hide_from_ui=True）。
        7. 返回 Command(goto=END, update={"messages": messages_update})。

        为什么要补占位 ToolMessage：
        若 state 里存在 AIMessage(tool_calls=[...]) 而缺对应 ToolMessage，
        checkpointer 会保存一份不完整历史，下次 resume 时 LLM 直接 400。
        所以在跳 END 前把配对补齐，保证 checkpoint 里的历史合法。

        Args:
            state:   当前 ThreadState，读取 messages。
            runtime: LangGraph 运行时，取 run_id / journal。

        Returns:
            Command(goto=END, update={"messages": [...]})；不暂停时返回 None。
        """
        tracker = self._get_tracker(runtime)
        if not tracker.stuck:
            return None

        if is_interrupt_protected():
            return None

        if self._help_count(runtime) >= self._max_help:
            return self._force_finalize(state, runtime, tracker)

        self._increment_help(runtime)
        journal = _get_runtime_value(runtime, "journal", None)
        run_id = _get_runtime_value(runtime, "run_id", None)
        reason = tracker.get_stuck_reason() or "unknown"
        if journal is not None:
            journal.append("help.requested", {
                "run_id": run_id, "reason": reason,
                "failures": len(tracker.get_failures()),
            })

        from langchain_core.messages import AIMessage

        tracker.reset()

        # Before jumping to END, patch any dangling tool_calls with placeholder
        # ToolMessages so the checkpointer saves a well-formed message history.
        # If we skip ToolNode while AIMessage(tool_calls=[...]) is in state,
        # the next run restores the checkpoint and immediately gets a 400 from LLM.
        extra_patches: list = []
        messages = state.get("messages") or []
        answered_ids: set = set()
        for msg in messages:
            if isinstance(msg, ToolMessage):
                answered_ids.add(msg.tool_call_id)
        for msg in messages:
            if not isinstance(msg, AIMessage):
                continue
            for tc in (getattr(msg, "tool_calls", None) or []):
                tc_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
                if tc_id and tc_id not in answered_ids:
                    extra_patches.append(ToolMessage(
                        content=f"[Skipped — stall detected ({reason})]",
                        tool_call_id=tc_id,
                        name=tc.get("name", "unknown") if isinstance(tc, dict) else getattr(tc, "name", "unknown"),
                    ))
                    answered_ids.add(tc_id)

        messages_update = extra_patches + [HumanMessage(
            content=f"[STALL DETECTED] {reason}. Pausing for user help.",
            name="stall_detection",
            additional_kwargs={"hide_from_ui": True},
        )]
        return Command(goto=END, update={"messages": messages_update})

    def _force_finalize(
        self, state: ThreadState, runtime: Runtime, tracker: StallTracker,
    ) -> Command:
        """求助次数用尽后的强制收尾。

        处理流程：
        1. journal.append("help.exhausted", {run_id})（有 journal 时）。
        2. 与 _check_stuck_and_pause 相同逻辑补齐悬空 tool_call，
           占位 ToolMessage content 标 "[Skipped — help exhausted]"。
        3. 追加一条隐藏 HumanMessage
           （"[HELP EXHAUSTED] Maximum help requests reached. Forcing finalization."）。
        4. 返回 Command(goto=END, update={"messages": extra_patches + [HumanMessage]})。

        Args:
            state:   当前 ThreadState，读取 messages。
            runtime: LangGraph 运行时，取 run_id / journal。
            tracker: 当前 run 的 StallTracker（形参保留，当前未直接使用）。

        Returns:
            Command(goto=END, update={"messages": [...]})。
        """
        from langchain_core.messages import AIMessage as _AIMessage

        journal = _get_runtime_value(runtime, "journal", None)
        run_id = _get_runtime_value(runtime, "run_id", None)
        if journal is not None:
            journal.append("help.exhausted", {"run_id": run_id})

        extra_patches: list = []
        messages = state.get("messages") or []
        answered_ids: set = set()
        for msg in messages:
            if isinstance(msg, ToolMessage):
                answered_ids.add(msg.tool_call_id)
        for msg in messages:
            if not isinstance(msg, _AIMessage):
                continue
            for tc in (getattr(msg, "tool_calls", None) or []):
                tc_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
                if tc_id and tc_id not in answered_ids:
                    extra_patches.append(ToolMessage(
                        content="[Skipped — help exhausted]",
                        tool_call_id=tc_id,
                        name=tc.get("name", "unknown") if isinstance(tc, dict) else getattr(tc, "name", "unknown"),
                    ))
                    answered_ids.add(tc_id)

        return Command(goto=END, update={"messages": extra_patches + [HumanMessage(
            content="[HELP EXHAUSTED] Maximum help requests reached. Forcing finalization.",
            name="stall_detection", additional_kwargs={"hide_from_ui": True},
        )]})

    @hook_config(can_jump_to=["end"])
    @override
    def after_model(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:
        """同步 after_model：记录 todo 状态；pending_stuck 为真则暂停。

        处理流程：
        1. 取当前 run 的 tracker；若 state.todos 非空 → tracker.record_todo_state(todos)。
        2. 取 run_id；若 _pending_stuck[run_id] 为真：
           - 清除该标记；
           - 调 _check_stuck_and_pause——
             · 有结果：把 result.update 拷贝出来并加 "jump_to": "end" 后返回；
             · 无结果：返回 None。
        3. 否则返回 None。

        hook_config(can_jump_to=["end"]) 允许这个 hook 跳到 end 节点。

        Args:
            state:   当前 ThreadState，读取 todos。
            runtime: LangGraph 运行时，取 run_id。

        Returns:
            含 messages 与 jump_to="end" 的 state patch；无需暂停时返回 None。
        """
        tracker = self._get_tracker(runtime)
        todos = state.get("todos") or []
        if todos:
            tracker.record_todo_state(todos)
        run_id = _get_runtime_value(runtime, "run_id", None) or "default"
        if self._pending_stuck.get(run_id):
            self._pending_stuck[run_id] = False
            result = self._check_stuck_and_pause(state, runtime)
            if result is not None:
                update = dict(result.update)
                update["jump_to"] = "end"
                return update
        return None

    @hook_config(can_jump_to=["end"])
    @override
    async def aafter_model(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:
        """异步 after_model：直接转调同步版，保证行为一致。"""
        return self.after_model(state, runtime)

    @override
    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        """同步 wrap_tool_call：记录工具失败 / 成功衰减；不在此暂停。

        处理流程：
        1. 取 tool_name / tool_input。
        2. handler(request)——
           - 异常：runtime 存在则 _check_and_flag_stuck(..., error=str(exc))，
             然后重新抛出（不在此暂停，交给 after_model）。
           - 正常：
             · 若 result 是 status="error" 的 ToolMessage：
               _check_and_flag_stuck(..., error=str(result.content))；
             · 否则若 result 是 ToolMessage：tracker.record_tool_success()，
               用于衰减陈旧的失败信号。
        3. 返回 result。

        为什么不在 wrap_tool_call 暂停：
        并行 tool_calls 时若在此暂停，会打断 ToolMessage 配对。暂停统一放
        after_model（那时 ToolMessage 已全部就位）。

        Args:
            request: 工具调用请求。
            handler: 下游处理函数。

        Returns:
            handler 返回的结果（异常时向上抛）。
        """
        tool_call = getattr(request, "tool_call", None) or {}
        tool_name = tool_call.get("name", "") if isinstance(tool_call, dict) else ""
        tool_input = tool_call.get("args", {}) if isinstance(tool_call, dict) else {}

        try:
            result = handler(request)
        except Exception as exc:
            runtime = getattr(request, "runtime", None)
            if runtime is not None:
                self._check_and_flag_stuck(runtime, tool_name, tool_input, str(exc))
            raise

        if isinstance(result, ToolMessage) and getattr(result, "status", None) == "error":
            runtime = getattr(request, "runtime", None)
            if runtime is not None:
                self._check_and_flag_stuck(runtime, tool_name, tool_input, str(result.content))
        elif isinstance(result, ToolMessage):
            # Successful ToolMessage — decay stale failure signals.
            runtime = getattr(request, "runtime", None)
            if runtime is not None:
                self._get_tracker(runtime).record_tool_success()
        return result

    @override
    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        """异步 wrap_tool_call：逻辑与同步版一致，仅 handler 改为 await。

        处理流程：
        1. 取 tool_name / tool_input。
        2. await handler(request)——
           - 异常：_check_and_flag_stuck(..., error=str(exc)) 后重新抛出；
           - 正常：
             · status="error" 的 ToolMessage → _check_and_flag_stuck；
             · 其他 ToolMessage → tracker.record_tool_success() 衰减。
        3. 返回 result。

        Args:
            request: 工具调用请求。
            handler: 下游异步处理函数。

        Returns:
            handler 返回的结果（异常时向上抛）。
        """
        tool_call = getattr(request, "tool_call", None) or {}
        tool_name = tool_call.get("name", "") if isinstance(tool_call, dict) else ""
        tool_input = tool_call.get("args", {}) if isinstance(tool_call, dict) else {}

        try:
            result = await handler(request)
        except Exception as exc:
            runtime = getattr(request, "runtime", None)
            if runtime is not None:
                self._check_and_flag_stuck(runtime, tool_name, tool_input, str(exc))
            raise

        if isinstance(result, ToolMessage) and getattr(result, "status", None) == "error":
            runtime = getattr(request, "runtime", None)
            if runtime is not None:
                self._check_and_flag_stuck(runtime, tool_name, tool_input, str(result.content))
        elif isinstance(result, ToolMessage):
            runtime = getattr(request, "runtime", None)
            if runtime is not None:
                self._get_tracker(runtime).record_tool_success()
        return result