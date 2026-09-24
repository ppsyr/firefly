"""TodoMiddleware — 扩展 TodoListMiddleware，增加上下文丢失检测与 Nag 提醒。

【三层保护】
1. before_model：上下文丢失检测
   —— write_todos 的调用被上下文截断后，历史里看不到 todo，
      此时注入一条提醒，让模型知道「你有未完成的 todos」。
2. after_model：完成度强制
   —— todos 未完成但 LLM 想直接给最终答案时，跳回 model 节点，
      最多强制 2 次（防止无限循环）。
3. before_model：Nag（双阈值提醒）
   —— steps_since_write >= 5 防「忘记」：
        模型埋头干活，很久没碰 todos。
      steps_since_reminder >= 5 防「打扰」：
        模型正在做子任务时，不要每一步都提醒。
   两个条件必须同时满足（AND）才触发；触发后只重置 steps_since_reminder。

【继承关系】
继承 TodoListMiddleware，复用基类的 write_todos 工具注册与 system prompt 注入。
"""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable
from typing import Any, override

from langchain.agents.middleware import TodoListMiddleware
from langchain.agents.middleware.todo import Todo
from langchain.agents.middleware.types import ModelRequest, ModelResponse, hook_config
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.runtime import Runtime

from poirot.backend.agents.middlewares.run_journal_middleware import _get_runtime_value
from poirot.backend.agents.middlewares import _jump_budget
from poirot.backend.agents.prompts import get_prompt_manager
from poirot.backend.agents.state.types import ThreadState

_STEPS_SINCE_WRITE_THRESHOLD = 5      # 距上次 write_todos 的步数阈值（防忘记）
_STEPS_SINCE_REMINDER_THRESHOLD = 5   # 距上次 Nag 提醒的步数阈值（防打扰）
_NAG_MIN_TODOS = 3                    # 触发 Nag 的最少 todo 数量
_MAX_COMPLETION_REMINDERS = 2         # 完成度强制的最大次数（防死循环）


def _todos_in_messages(messages: list[Any]) -> bool:
    """历史里是否出现过 write_todos 的 tool_call。

    遍历消息，找 AIMessage 的 tool_calls 中是否有名为 "write_todos" 的调用；
    有则 True，否则 False。

    Args:
        messages: 消息列表。

    Returns:
        是否存在 write_todos 调用。
    """
    for msg in messages:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for tc in msg.tool_calls:
                if tc.get("name") == "write_todos":
                    return True
    return False


def _count_write_todos(messages: list[Any]) -> int:
    """统计历史里 write_todos tool_call 的总次数。

    用于判断本轮是否「刚写过 todos」——次数增加即视为刚写过。

    Args:
        messages: 消息列表。

    Returns:
        write_todos 调用次数。
    """
    count = 0
    for msg in messages:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for tc in msg.tool_calls:
                if tc.get("name") == "write_todos":
                    count += 1
    return count


def _reminder_in_messages(messages: list[Any]) -> bool:
    """历史里是否已经注入过 todo_reminder（name="todo_reminder"）。

    用于避免上下文丢失提醒重复注入。

    Args:
        messages: 消息列表。

    Returns:
        是否已存在 todo_reminder。
    """
    for msg in messages:
        if isinstance(msg, HumanMessage) and getattr(msg, "name", None) == "todo_reminder":
            return True
    return False


def _format_todos(todos: list[Todo]) -> str:
    """把 todos 格式化成 "- [status] content" 多行文本。"""
    return "\n".join(f"- [{t.get('status', 'pending')}] {t.get('content', '')}" for t in todos)


def _format_completion_reminder(todos: list[Todo]) -> str:
    """构造「完成度提醒」文本，只列未完成的 todo。

    先过滤出 status != "completed" 的项，格式化后交给
    prompt manager 渲染 "todo/completion_reminder" 模板。

    Args:
        todos: 全部 todos。

    Returns:
        渲染后的提醒文本。
    """
    incomplete = [t for t in todos if t.get("status") != "completed"]
    lines = "\n".join(f"- [{t.get('status', 'pending')}] {t.get('content', '')}" for t in incomplete)
    return get_prompt_manager().load("todo", "completion_reminder", lines=lines)


def _infer_current_step_id(last_ai: AIMessage | None) -> str | None:
    """从最新 AIMessage 的 write_todos tool_call 解析 in_progress 项，派生 step_id。

    处理流程：
    1. last_ai 为空或无 tool_calls → None。
    2. 遍历 tool_calls，找 name == "write_todos" 的调用。
    3. 取其 args.todos 列表，找第一个 status == "in_progress" 的项，
       返回 f"todo-{index}"。
    4. 没有 in_progress 项 → None。

    Args:
        last_ai: 最新的 AIMessage，可为 None。

    Returns:
        形如 "todo-{index}" 的步骤 id；无则 None。
    """
    if not last_ai or not getattr(last_ai, "tool_calls", None):
        return None
    for tc in last_ai.tool_calls:
        if tc.get("name") == "write_todos":
            todos = (tc.get("args") or {}).get("todos", []) or []
            for idx, t in enumerate(todos):
                if t.get("status") == "in_progress":
                    return f"todo-{idx}"
            return None
    return None


def _has_tool_call_intent(message: AIMessage) -> bool:
    """判断 AIMessage 是否带有「想调用工具」的意图。

    检查多个来源，命中任一即视为有工具调用意图：
    - message.tool_calls 非空；
    - message.invalid_tool_calls 非空；
    - additional_kwargs 含 tool_calls / function_call；
    - response_metadata.finish_reason ∈ {"tool_calls", "function_call"}。

    用途：只有「干净最终答案」（无工具调用意图）才进行完成度强制。

    Args:
        message: AIMessage。

    Returns:
        是否有工具调用意图。
    """
    if message.tool_calls:
        return True
    if getattr(message, "invalid_tool_calls", None):
        return True
    additional_kwargs = getattr(message, "additional_kwargs", {}) or {}
    if additional_kwargs.get("tool_calls") or additional_kwargs.get("function_call"):
        return True
    response_metadata = getattr(message, "response_metadata", {}) or {}
    return response_metadata.get("finish_reason") in {"tool_calls", "function_call"}


def _has_persistent_failures(state: Any) -> bool:
    """判断 errors 是否有 tool 持续失败超阈（attempt >= 3）。

    从 state.errors 派生：对每个 tool 取其最新条目的 attempt，
    任一 tool 的 attempt >= 3 即视为持续失败。
    用途：工具持续失败时放行退出，不强制 all-completed，
    让任务跑到结尾产一份带缺口的报告。

    Args:
        state: 当前 state（需为 dict）。

    Returns:
        是否存在持续失败的工具。
    """
    if not isinstance(state, dict):
        return False
    errors = state.get("errors") or []
    latest: dict[str, int] = {}
    for err in errors:
        tn = _err_field(err, "tool_name")
        att = _err_field(err, "attempt")
        if tn and att is not None:
            latest[tn] = int(att)
    return any(att >= 3 for att in latest.values())


def _err_field(item: Any, name: str) -> Any:
    """统一字段访问：dict 用 get，其他对象用 getattr（无则 None）。"""
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


class TodoMiddleware(TodoListMiddleware):
    """扩展 TodoListMiddleware：上下文丢失检测 + 完成度强制。

    继承基类的 write_todos 工具注册与 system-prompt 注入。

    内部按 (thread_id, run_id) 或 thread_id 维护多张表：
    - _pending_completion_reminders：待注入的完成度提醒；
    - _completion_reminder_counts：  完成度强制已用次数；
    - _steps_since_write：           距上次 write_todos 的步数；
    - _steps_since_reminder：        距上次 Nag 提醒的步数；
    - _last_write_count：            上次记录的 write_todos 总次数。
    """

    state_schema = ThreadState  # type: ignore[assignment]

    def __init__(self, *args: Any, enforce_completion: bool = True, **kwargs: Any) -> None:
        """初始化。

        Args:
            *args / **kwargs: 透传给 TodoListMiddleware。
            enforce_completion: 是否启用完成度强制（Layer 2）。
                False → default 模式：不强制完成度，模型想退就退；
                软引导（prompt + write_todos 注册 + context-loss 检测）仍保留。
        """
        super().__init__(*args, **kwargs)
        self._enforce_completion = enforce_completion
        self._lock = threading.Lock()
        self._pending_completion_reminders: dict[tuple[str, str], list[str]] = {}
        self._completion_reminder_counts: dict[tuple[str, str], int] = {}
        self._steps_since_write: dict[str, int] = {}
        self._steps_since_reminder: dict[str, int] = {}
        self._last_write_count: dict[str, int] = {}

    @staticmethod
    def _get_thread_id(runtime: Runtime) -> str:
        """取 thread_id（缺省 "default"）。"""
        tid = _get_runtime_value(runtime, "thread_id", None)
        return str(tid) if tid else "default"

    @staticmethod
    def _get_run_id(runtime: Runtime) -> str:
        """取 run_id（缺省 "default"）。"""
        rid = _get_runtime_value(runtime, "run_id", None)
        return str(rid) if rid else "default"

    def _pending_key(self, runtime: Runtime) -> tuple[str, str]:
        """待注入提醒 / 计数表的键：(thread_id, run_id)。"""
        return self._get_thread_id(runtime), self._get_run_id(runtime)

    @override
    def before_model(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:
        """同步 before_model：上下文丢失检测（Layer 1）+ Nag（Layer 3）。

        处理流程：
        1. 取 todos / messages。
        2. Layer 1（上下文丢失检测）：
           若 todos 非空、messages 里没有 write_todos、也没有 todo_reminder
           → 注入一条 todo_reminder（内容由 prompt manager 渲染
             "todo/context_loss_reminder"），hide_from_ui=True，直接返回。
        3. Layer 3（Nag，双阈值）：
           - 取 thread_id / incomplete（未完成 todos）；
           - write_count = _count_write_todos(messages)；
           - 加锁更新：
             · 若 write_count 比上次大 → 刚写过：steps_since_write=0，
               并更新 last_write_count；
             · 否则 steps_since_write += 1；
             · steps_since_reminder += 1；
           - should_nag = 有未完成 且 len(todos) >= 3
                          且 steps_since_write >= 5
                          且 steps_since_reminder >= 5；
           - 若 should_nag：steps_since_reminder = 0，记下 steps。
        4. 若 should_nag：注入 todo_nag（prompt "todo/nag_reminder"），
           hide_from_ui=True。
        5. 都不触发 → None。

        Args:
            state:   当前 ThreadState，读取 todos / messages。
            runtime: LangGraph 运行时，读取 thread_id / run_id。

        Returns:
            含注入 HumanMessage 的 state patch；无注入时返回 None。
        """
        todos: list[Todo] = state.get("todos") or []  # type: ignore[assignment]
        messages = state.get("messages") or []

        # Layer 1: context-loss detection
        if todos and not _todos_in_messages(messages) and not _reminder_in_messages(messages):
            return {"messages": [HumanMessage(
                name="todo_reminder",
                additional_kwargs={"hide_from_ui": True},
                content=get_prompt_manager().load("todo", "context_loss_reminder", todos=_format_todos(todos)),
            )]}

        # Layer 3: Nag — dual-threshold reminder.
        thread_id = self._get_thread_id(runtime)
        incomplete = [t for t in todos if t.get("status") != "completed"]
        write_count = _count_write_todos(messages)
        with self._lock:
            if write_count > self._last_write_count.get(thread_id, 0):
                self._last_write_count[thread_id] = write_count
                self._steps_since_write[thread_id] = 0
            else:
                self._steps_since_write[thread_id] = self._steps_since_write.get(thread_id, 0) + 1
            self._steps_since_reminder[thread_id] = self._steps_since_reminder.get(thread_id, 0) + 1

            should_nag = (
                bool(incomplete)
                and len(todos) >= _NAG_MIN_TODOS
                and self._steps_since_write[thread_id] >= _STEPS_SINCE_WRITE_THRESHOLD
                and self._steps_since_reminder[thread_id] >= _STEPS_SINCE_REMINDER_THRESHOLD
            )
            if should_nag:
                self._steps_since_reminder[thread_id] = 0
                steps = self._steps_since_write[thread_id]

        if should_nag:
            return {"messages": [HumanMessage(
                name="todo_nag",
                additional_kwargs={"hide_from_ui": True},
                content=get_prompt_manager().load("todo", "nag_reminder", steps=steps, lines=_format_todos(incomplete)),
            )]}

        return None

    @override
    async def abefore_model(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:
        """异步 before_model：直接转调同步版，保证行为一致。"""
        return self.before_model(state, runtime)

    # ------------------------------------------------------------------ #
    # Layer 2: completion enforcement                                       #
    # ------------------------------------------------------------------ #

    def _queue_completion_reminder(self, runtime: Runtime, reminder: str) -> None:
        """把一条完成度提醒入队，并把该 (thread, run) 的强制次数 +1。"""
        key = self._pending_key(runtime)
        with self._lock:
            self._pending_completion_reminders.setdefault(key, []).append(reminder)
            self._completion_reminder_counts[key] = self._completion_reminder_counts.get(key, 0) + 1

    def _drain_completion_reminders(self, runtime: Runtime) -> list[str]:
        """取出并清空当前 (thread, run) 的待注入完成度提醒。"""
        key = self._pending_key(runtime)
        with self._lock:
            return self._pending_completion_reminders.pop(key, [])

    def _completion_reminder_count(self, runtime: Runtime) -> int:
        """取当前 (thread, run) 已强制完成的次数。"""
        key = self._pending_key(runtime)
        with self._lock:
            return self._completion_reminder_counts.get(key, 0)

    def _clear_run_state(self, runtime: Runtime) -> None:
        """清空当前 (thread, run) 的完成度提醒与计数。"""
        key = self._pending_key(runtime)
        with self._lock:
            self._pending_completion_reminders.pop(key, None)
            self._completion_reminder_counts.pop(key, None)

    def _clear_other_runs(self, runtime: Runtime) -> None:
        """清空同一 thread 下、其他 run 的陈旧状态（避免旧 run 残留污染）。"""
        thread_id, current_run_id = self._pending_key(runtime)
        with self._lock:
            stale = [k for k in list(self._pending_completion_reminders) if k[0] == thread_id and k[1] != current_run_id]
            for k in stale:
                self._pending_completion_reminders.pop(k, None)
                self._completion_reminder_counts.pop(k, None)

    def _reset_nag_counters(self, runtime: Runtime) -> None:
        """重置当前 thread 的 Nag 相关计数（steps_since_write / steps_since_reminder / last_write_count）。"""
        thread_id = self._get_thread_id(runtime)
        with self._lock:
            self._steps_since_write.pop(thread_id, None)
            self._steps_since_reminder.pop(thread_id, None)
            self._last_write_count.pop(thread_id, None)

    @hook_config(can_jump_to=["model"])
    @override
    def after_model(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:
        """同步 after_model：完成度强制（Layer 2）。

        处理流程：
        1. 先调基类 after_model（并行 write_todos 检测）；有结果直接返回。
        2. 从 messages 里取最后一条 AIMessage。
        3. 从 write_todos 调用解析 in_progress → step_id；
           有则 step_update = {"current_step_id": step_id}。
        4. 若没有 last_ai 或 last_ai 有工具调用意图 → 返回 step_update 或 None
           （只拦截「干净的最终答案」）。
        5. 若 enforce_completion 为 False（default 模式）→ 返回 step_update 或 None
           （不强制完成度，模型想退就退）。
        6. 若 todos 为空或全部 completed → 返回 step_update 或 None（允许退出）。
        7. 若 _has_persistent_failures(state) → 返回 step_update 或 None
           （工具持续失败时不强制 all-completed，让任务收尾产缺口报告）。
        8. 若完成度强制次数 >= _MAX_COMPLETION_REMINDERS → 返回 step_update 或 None
           （防止无限循环）。
        9. 若 _jump_budget.try_consume(runtime) 失败 → 返回 step_update 或 None
           （与 Reflection 共享 jump 预算，合计 ≤3）。
        10. 入队完成度提醒；返回 {"jump_to": "model", **step_update}
            （跳回 model 节点，让模型继续工作）。

        hook_config(can_jump_to=["model"]) 允许这个 hook 跳到 model 节点。

        Args:
            state:   当前 ThreadState，读取 messages / todos / errors。
            runtime: LangGraph 运行时，读取 thread_id / run_id。

        Returns:
            {"jump_to": "model", "current_step_id": ...} 或 None。
        """
        # 1. Preserve base class logic (parallel write_todos detection).
        base_result = super().after_model(state, runtime)
        if base_result is not None:
            return base_result

        messages = state.get("messages") or []
        last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)

        # 2. 推断 current_step_id（从 write_todos 调用解析 in_progress 项，D10）。
        step_id = _infer_current_step_id(last_ai)
        step_update: dict[str, Any] = {"current_step_id": step_id} if step_id is not None else {}

        # 3. Only intercept clean final answers (no tool-call intent).
        if not last_ai or _has_tool_call_intent(last_ai):
            return step_update or None

        # default 模式（enforce_completion=False）：不强制完成度，模型想退就退。
        # 软引导（prompt + write_todos 工具注册 + context-loss 检测）仍保留。
        if not self._enforce_completion:
            return step_update or None

        # 4. Allow exit when all todos are completed or none exist.
        todos: list[Todo] = state.get("todos") or []  # type: ignore[assignment]
        if not todos or all(t.get("status") == "completed" for t in todos):
            return step_update or None

        # 4b. F8.4：失败超阈放行——工具持续失败时不强制 all-completed，让任务跑到结尾产带缺口报告。
        if _has_persistent_failures(state):
            return step_update or None

        # 5. Enforce reminder cap to prevent infinite loops.
        if self._completion_reminder_count(runtime) >= _MAX_COMPLETION_REMINDERS:
            return step_update or None

        # 6. 共享 jump 预算门（与 Reflection 合计 ≤3，D6）。
        if not _jump_budget.try_consume(runtime):
            return step_update or None

        # 7. Queue reminder and jump back to model node.
        self._queue_completion_reminder(runtime, _format_completion_reminder(todos))
        return {"jump_to": "model", **step_update}

    @hook_config(can_jump_to=["model"])
    @override
    async def aafter_model(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:
        """异步 after_model：直接转调同步版，保证行为一致。"""
        return self.after_model(state, runtime)

    # ------------------------------------------------------------------ #
    # wrap_model_call: inject queued completion reminders                  #
    # ------------------------------------------------------------------ #

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """同步 wrap_model_call：把排队的完成度提醒注入送 LLM 的 messages。

        处理流程：
        1. _drain_completion_reminders(request.runtime) 取待注入提醒。
        2. 无提醒 → 直接 super().wrap_model_call(request, handler)。
        3. 有提醒 → 去重后用 "\\n\\n" 拼成一条 HumanMessage
           （name="todo_completion_reminder"，hide_from_ui=True），
           追加到 request.messages 末尾，
           再调 super().wrap_model_call(request.override(messages=new_messages), handler)。

        Args:
            request: 模型调用请求。
            handler: 下游处理函数。

        Returns:
            handler 返回的模型调用结果。
        """
        reminders = self._drain_completion_reminders(request.runtime)
        if not reminders:
            return super().wrap_model_call(request, handler)
        new_messages = [
            *request.messages,
            HumanMessage(
                content="\n\n".join(dict.fromkeys(reminders)),
                name="todo_completion_reminder",
                additional_kwargs={"hide_from_ui": True},
            ),
        ]
        return super().wrap_model_call(request.override(messages=new_messages), handler)

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """异步 wrap_model_call：逻辑与同步版一致，仅 handler 改为 await。

        Args:
            request: 模型调用请求。
            handler: 下游异步处理函数。

        Returns:
            下游返回的模型调用结果。
        """
        reminders = self._drain_completion_reminders(request.runtime)
        if not reminders:
            return await super().awrap_model_call(request, handler)
        new_messages = [
            *request.messages,
            HumanMessage(
                content="\n\n".join(dict.fromkeys(reminders)),
                name="todo_completion_reminder",
                additional_kwargs={"hide_from_ui": True},
            ),
        ]
        return await super().awrap_model_call(request.override(messages=new_messages), handler)

    # ------------------------------------------------------------------ #
    # per-run state lifecycle                                              #
    # ------------------------------------------------------------------ #

    @override
    def before_agent(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:
        """同步 before_agent：清理其他 run 的陈旧状态 + 重置 Nag 计数。

        Args:
            state:   当前 ThreadState（未使用）。
            runtime: LangGraph 运行时，读取 thread_id / run_id。

        Returns:
            始终 None。
        """
        self._clear_other_runs(runtime)
        self._reset_nag_counters(runtime)
        return None

    @override
    async def abefore_agent(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:
        """异步 before_agent：与同步版一致。"""
        self._clear_other_runs(runtime)
        self._reset_nag_counters(runtime)
        return None

    @override
    def after_agent(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:
        """同步 after_agent：清空当前 run 的完成度状态 + 清空 jump 预算。

        Args:
            state:   当前 ThreadState（未使用）。
            runtime: LangGraph 运行时，读取 thread_id / run_id。

        Returns:
            始终 None。
        """
        self._clear_run_state(runtime)
        _jump_budget.clear(runtime)
        return None

    @override
    async def after_agent(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:
        """异步 after_agent：与同步版一致。"""
        self._clear_run_state(runtime)
        _jump_budget.clear(runtime)
        return None