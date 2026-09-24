"""RunJournalMiddleware — 把 agent / model / tool 生命周期事件记入 RunJournal。

【整体职责】
以 AgentMiddleware 形式（取代旧 BaseMiddleware 体系）监听 agent / model / tool
的生命周期 hook，把关键事件追加写入 RunJournal，供审计、回放、UI 活动展示使用。

【内容摘要】
- _get_runtime_value : 从 runtime 多路径提取 configurable 值（兼容 LangGraph ≥1.1.9）。
- _tool_text         : 把 tool 结果压平成字符串。
- _result_status     : 从 tool 结果识别错误信号（ok / error）。
- _truncate_tool_input : 生成 tool input 短摘要（≤80 字符）。
- RunJournalMiddleware : 记录 agent / model / tool 事件到 RunJournal。

【职责边界】
- 只负责：监听生命周期 hook 并写 journal、向 activity_tracker 报活动。
- 不负责：journal 的读写实现（journal 模块）、活动追踪实现（observability）、
  runtime 的构造与 configurable 注入（bootstrap / runtime）。

【依赖获取方式】
journal / run_id / activity_tracker / model 等依赖都从 runtime configurable 读取，
统一走模块级函数 _get_runtime_value（它内部走三到四条路径，兼容 LangGraph ≥1.1.9
与测试用的 SimpleNamespace）。

【模块级函数的复用】
_get_runtime_value 为模块级函数，除本文件外，TodoMiddleware / _jump_budget
等也复用同一套取值逻辑，避免各处重复实现。

【无 journal 时的行为】
任何 hook 里 journal 为 None 都静默跳过，不报错（容忍运行环境缺件）。

【INVARIANT】
- journal 为 None 时全部 hook 静默跳过，不报错。
- 依赖统一走 _get_runtime_value（多路径兼容）。
- 事件类型固定：agent.started / agent.finished / llm.request / llm.response /
  tool.called / tool.finished。
- tool.finished 的 status 由 _result_status 判定（不只看异常）。
- 异常路径：先记 journal（status=error）+ tracker.finish(error=)，再 re-raise。
- output 截断 2000 字符；tool input 摘要截断 80 字符。
- wrap_tool_call 作为 wrap 外层，能看到内层中间件处理后的最终结果。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, override

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.runtime import Runtime


def _get_runtime_value(runtime: Any, key: str, default: Any = None) -> Any:
    """从 runtime 中提取 configurable 值（多路径兼容）。

    按优先级依次尝试以下路径，命中即返回：
    1. runtime.context（dict 或对象）—— 配置了 context_schema 时使用。
    2. langgraph.config.get_config()["configurable"] —— create_agent 标准
       hook 内访问 config 的方式（F2 修复）。
    3. runtime.get_configurable() —— LangGraph runtime API（若存在）。
    4. runtime.config["configurable"] —— 兜底路径。

    任一路径抛异常都被吞掉，继续尝试下一条；全部失败返回 default。

    Args:
        runtime: LangGraph 运行时对象，可为 None。
        key: 要取的 configurable 键名。
        default: 全部路径失败时的返回值。

    Returns:
        Any: 取到的值；取不到返回 default。
    """
    if runtime is not None:
        # Path 1: runtime.context
        ctx = getattr(runtime, "context", None)
        if ctx is not None:
            if isinstance(ctx, dict):
                if key in ctx:
                    return ctx[key]
            else:
                val = getattr(ctx, key, None)
                if val is not None:
                    return val
    # Path 2: get_config() —— create_agent hook 内取 configurable 的标准方式（F2 修复）
    try:
        from langgraph.config import get_config

        config = get_config()
        if config:
            configurable = config.get("configurable", {})
            if isinstance(configurable, dict) and key in configurable:
                return configurable[key]
    except Exception:
        pass
    # Path 3: runtime.get_configurable()
    if runtime is not None:
        get_cfg = getattr(runtime, "get_configurable", None)
        if callable(get_cfg):
            try:
                cfg = get_cfg()
                if cfg and isinstance(cfg, dict) and key in cfg:
                    return cfg[key]
            except Exception:
                pass
        # Path 4: runtime.config["configurable"]
        config = getattr(runtime, "config", None)
        if isinstance(config, dict):
            configurable = config.get("configurable", {})
            if isinstance(configurable, dict) and key in configurable:
                return configurable[key]
    return default


def _tool_text(result: Any) -> str:
    """把 tool 调用结果压平成字符串，便于 journal 记录。

    支持形态：
    - ToolMessage：按其 content 形态处理——
        · str  → 原样；
        · list → 逐项拼接（dict 含 "text" 取该字段，否则 str(item)），
                 跳过假值项；
        · 其他 → str(content)。
    - 其他对象：str(result)。

    Args:
        result: tool 调用返回结果。

    Returns:
        str: 压平后的字符串。
    """
    if isinstance(result, ToolMessage):
        content = result.content
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                item["text"] if isinstance(item, dict) and "text" in item else str(item)
                for item in content if item
            )
        return str(content)
    return str(result)


def _result_status(result: Any) -> str:
    """从 tool 结果中识别错误信号（不仅限于 Python 异常）。

    Tool 可能不抛异常，但通过返回值表达失败：
    - ToolMessage(status="error")；
    - Command(update={"errors": [{"kind": "failure"}], ...})；
    - Command(update={"messages": [ToolMessage(status="error")]}）。

    这些情况 journal 必须记成 status="error"。

    Args:
        result: tool 调用返回结果。

    Returns:
        str: "error" 或 "ok"。
    """
    if isinstance(result, ToolMessage):
        if getattr(result, "status", None) == "error":
            return "error"
        return "ok"
    update = getattr(result, "update", None)
    if isinstance(update, dict):
        errors = update.get("errors")
        if isinstance(errors, list):
            for err in errors:
                if isinstance(err, dict) and err.get("kind") == "failure":
                    return "error"
        messages = update.get("messages")
        if isinstance(messages, list):
            for msg in messages:
                if isinstance(msg, ToolMessage) and getattr(msg, "status", None) == "error":
                    return "error"
    return "ok"


def _truncate_tool_input(tool_input: Any) -> str:
    """为活动展示生成 tool input 的短摘要（最多 80 字符）。

    - dict：优先 command，其次 path，再退回 str(tool_input)；
    - 其他：str(tool_input)。

    Args:
        tool_input: tool 调用入参。

    Returns:
        str: 截断到 80 字符的摘要字符串。
    """
    if isinstance(tool_input, dict):
        cmd = tool_input.get("command") or tool_input.get("path") or str(tool_input)
        return str(cmd)[:80]
    return str(tool_input)[:80]


class RunJournalMiddleware(AgentMiddleware):
    """记录 agent / model / tool 事件到 RunJournal。无 journal 时静默不报错。

    每个 hook 都从 runtime 取 journal；若为 None 则直接跳过打点。
    依赖获取统一走 _get_runtime_value，兼容多种 runtime 形态。
    """

    def _journal(self, runtime: Runtime) -> Any:
        """从 runtime 取 journal（无则 None）。"""
        return _get_runtime_value(runtime, "journal", None)

    def _run_id(self, runtime: Runtime) -> Any:
        """从 runtime 取 run_id（无则 None）。"""
        return _get_runtime_value(runtime, "run_id", None)

    def _activity_tracker(self, runtime: Runtime) -> Any:
        """从 runtime 取 activity_tracker（无则 None）。"""
        return _get_runtime_value(runtime, "activity_tracker", None)

    def _model_name(self, runtime: Runtime) -> str:
        """从 runtime 取 model 名，强转为字符串（无则空串）。"""
        return str(_get_runtime_value(runtime, "model", "") or "")

    @override
    def before_agent(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        """同步 before_agent hook：记录 agent.started 事件。"""
        journal = self._journal(runtime)
        if journal is not None:
            journal.append("agent.started", {"run_id": self._run_id(runtime)})
        return None

    @override
    def after_agent(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        """同步 after_agent hook：记录 agent.finished 事件。"""
        journal = self._journal(runtime)
        if journal is not None:
            journal.append("agent.finished", {"run_id": self._run_id(runtime)})
        return None

    @override
    def before_model(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        """同步 before_model hook：记录 llm.request 事件（run_id + model 名）。"""
        journal = self._journal(runtime)
        if journal is not None:
            journal.append("llm.request", {"run_id": self._run_id(runtime), "model": self._model_name(runtime)})
        return None

    @override
    def after_model(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        """同步 after_model hook：记录 llm.response 事件（run_id + model 名）。"""
        journal = self._journal(runtime)
        if journal is not None:
            journal.append("llm.response", {"run_id": self._run_id(runtime), "model": self._model_name(runtime)})
        return None

    @override
    def wrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        """同步 wrap_tool_call：记录 tool.called / tool.finished + 活动追踪。

        处理流程：
        1. 从 request.runtime 取 journal / run_id；从 request.tool_call 取
           tool_name / tool_input。
        2. 若 journal 非空：append "tool.called"（带 tool_input）。
        3. 若 activity_tracker 非空：start(activity_id, "tool", 摘要)，
           activity_id 形如 "tool_name:tool_call_id"。
        4. 调 handler(request)：
           - 成功：status = _result_status(result)（区分 ok / error）。
           - 抛异常：status = "error"，tracker.finish(error=...)，
             journal.append("tool.finished", status="error")，然后重新抛出。
        5. 成功路径：tracker.finish(status, output_size)，journal.append
           "tool.finished"（output 截断到 2000 字符）。
        6. 返回 result。

        Args:
            request: 工具调用请求（含 runtime / tool_call）。
            handler: 下游处理函数。

        Returns:
            Any: handler 返回的结果（异常时向上抛）。
        """
        runtime = getattr(request, "runtime", None)
        journal = self._journal(runtime) if runtime is not None else None
        run_id = self._run_id(runtime) if runtime is not None else None
        tool_call = getattr(request, "tool_call", None) or {}
        tool_name = tool_call.get("name", "") if isinstance(tool_call, dict) else ""
        tool_input = tool_call.get("args", {}) if isinstance(tool_call, dict) else {}

        if journal is not None:
            journal.append("tool.called", {
                "run_id": run_id,
                "tool_name": tool_name,
                "tool_input": tool_input,
            })
        tracker = self._activity_tracker(runtime) if runtime is not None else None
        activity_id = f"{tool_name}:{tool_call.get('id', '')}" if isinstance(tool_call, dict) else tool_name
        if tracker is not None:
            tracker.start(activity_id, "tool", f"{tool_name}: {_truncate_tool_input(tool_input)}")
        try:
            result = handler(request)
            status = _result_status(result)
        except Exception as exc:
            status = "error"
            if tracker is not None:
                tracker.finish(activity_id, status="error", error=str(exc))
            if journal is not None:
                journal.append("tool.finished", {
                    "run_id": run_id,
                    "tool_name": tool_name,
                    "output": str(exc),
                    "status": status,
                })
            raise
        if tracker is not None:
            tracker.finish(activity_id, status=status, output_size=len(_tool_text(result)))
        if journal is not None:
            journal.append("tool.finished", {
                "run_id": run_id,
                "tool_name": tool_name,
                "output": _tool_text(result)[:2000],
                "status": status,
            })
        return result

    @override
    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        """异步 wrap_tool_call：逻辑同 wrap_tool_call，使用 await handler。

        Args:
            request: 工具调用请求（含 runtime / tool_call）。
            handler: 下游异步处理函数。

        Returns:
            Any: handler 返回的结果（异常时向上抛）。
        """
        runtime = getattr(request, "runtime", None)
        journal = self._journal(runtime) if runtime is not None else None
        run_id = self._run_id(runtime) if runtime is not None else None
        tool_call = getattr(request, "tool_call", None) or {}
        tool_name = tool_call.get("name", "") if isinstance(tool_call, dict) else ""
        tool_input = tool_call.get("args", {}) if isinstance(tool_call, dict) else {}

        if journal is not None:
            journal.append("tool.called", {
                "run_id": run_id,
                "tool_name": tool_name,
                "tool_input": tool_input,
            })
        tracker = self._activity_tracker(runtime) if runtime is not None else None
        activity_id = f"{tool_name}:{tool_call.get('id', '')}" if isinstance(tool_call, dict) else tool_name
        if tracker is not None:
            tracker.start(activity_id, "tool", f"{tool_name}: {_truncate_tool_input(tool_input)}")
        try:
            result = await handler(request)
            status = _result_status(result)
        except Exception as exc:
            status = "error"
            if tracker is not None:
                tracker.finish(activity_id, status="error", error=str(exc))
            if journal is not None:
                journal.append("tool.finished", {
                    "run_id": run_id,
                    "tool_name": tool_name,
                    "output": str(exc),
                    "status": status,
                })
            raise
        if tracker is not None:
            tracker.finish(activity_id, status=status, output_size=len(_tool_text(result)))
        if journal is not None:
            journal.append("tool.finished", {
                "run_id": run_id,
                "tool_name": tool_name,
                "output": _tool_text(result)[:2000],
                "status": status,
            })
        return result