"""tools.py — 动态生成 specialist tool + subagent tool。

【整体职责】
为每个 specialist / subagent 动态生成一个 LangChain BaseTool（delegate_to_<name>），
供 Leader 的 LLM 调用。tool handler 内部完成整条派活编排：
ContextSummarizer → specialist.invoke → ResultSummarizer → 返回 JSON 字符串。
LLM 只需填 3 个参数（goal / success_criteria / sandbox_id 可选），其余内部处理。

【内容摘要】
- set_current_state() / get_current_state() : 通过 ContextVar 暴露 / 读取当前 ThreadState。
- _extract_sandbox_id()                     : 从 state 或显式参数解析 sandbox_id。
- _write_decision_log_async()               : 异步写 DecisionLogRecord（L3 扩展，fire-and-forget）。
- make_specialist_tool()                    : 为 specialist 生成 delegate_to_<name> 工具。
- make_subagent_tool()                      : 为 subagent 生成 delegate_to_subagent 工具。

【职责边界】
- 只负责：tool 生成、tool handler 内的派活编排、JSON 序列化、异常转 error JSON。
- 不负责：路由决策（LLM 负责）、打点（OrchestrationMiddleware 负责）、
  specialist 实际执行（runtime 负责）、上下文/结果摘要的实现（summarizer 负责）。
- 不持有状态：`_current_state` 是 ContextVar，随调用上下文隔离。

【INVARIANT】
- tool schema 精简：LLM 只填 goal + success_criteria（+ 可选 sandbox_id）。
- 派活编排顺序固定：ContextSummarizer → specialist.invoke → ResultSummarizer → JSON。
- specialist 失败抛 SpecialistError → 转为 error JSON（LLM 决策 retry/fallback），
  不向上抛异常；subagent 失败抛 SubagentError → 同样转 error JSON。
- ThreadState 通过 ContextVar 传递：由 OrchestrationMiddleware 在 handler 前
  调 set_current_state 写入；tool handler 内 get_current_state 读取。
- sandbox_id 解析优先级：显式参数 > state["sandbox"]["sandbox_id"]。
- 返回 JSON 字段固定：success / summary / specialist / gap_analysis / artifacts。
- L2 扩展：budget_guard 非 None 时先做预算检查，超限直接返回 BudgetExceeded JSON。
- L3 扩展：decision_log_writer 非 None 时异步写 DecisionLogRecord，不阻塞 L1 turn。
- 新增 specialist 只需注册到 Registry，本工厂自动生成 tool，零代码侵入。
"""
from __future__ import annotations

import contextvars
import json
from typing import Any

from langchain_core.tools import BaseTool, tool

from poirot.backend.agents.multiagent.context_summarizer import ContextSummarizer
from poirot.backend.agents.multiagent.exceptions import (
    SpecialistError,
    SubagentError,
)
from poirot.backend.agents.multiagent.result_summarizer import ResultSummarizer
from poirot.backend.agents.multiagent.specialist import SpecialistAgent
from poirot.backend.agents.multiagent.subagent import SubagentProvider
from poirot.backend.agents.multiagent.types import (
    SpecialistRequest,
    SubagentRequest,
)

# ContextVar：跨调用隔离的 ThreadState 传递通道。
# 由 OrchestrationMiddleware 在 handler 前写入，tool handler 内读取。
_current_state: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "multiagent_state", default=None,
)


def set_current_state(state: dict) -> None:
    """OrchestrationMiddleware 调用：设置当前 ThreadState 供 tool handler 读取。"""
    _current_state.set(state)


def get_current_state() -> dict:
    """tool handler 调用：获取当前 ThreadState（未设置时返回空 dict）。"""
    return _current_state.get() or {}


def _extract_sandbox_id(state: dict, sandbox_id: str | None) -> str | None:
    """解析 sandbox_id：显式参数优先，否则从 state["sandbox"] 取。"""
    if sandbox_id is not None:
        return sandbox_id
    sandbox = state.get("sandbox")
    if isinstance(sandbox, dict):
        return sandbox.get("sandbox_id")
    return None


def _write_decision_log_async(
    writer: Any, specialist_name: str, goal: str, success_criteria: str, result: Any,
) -> None:
    """异步写 DecisionLogRecord（L3 扩展，fire-and-forget）。

    - lazy import L3 类型，避免循环依赖。
    - failure_category 从 result 取（L2 ResultSummarizer 输出，可能为 None），
      字符串形式时尝试转 FailureCategory 枚举，失败则置 None。
    - lesson_text 在 MVP 不生成，留给 L3 后置分析。

    Args:
        writer: 决策日志 writer（提供 write_async 方法）。
        specialist_name: specialist 名。
        goal: 任务目标。
        success_criteria: 成功标准。
        result: ResultSummarizer 产出的 SpecialistResult。
    """
    import uuid
    from poirot.backend.agents.journal.events import utc_now_iso
    from poirot.backend.agents.multiagent.eval.types import DecisionLogRecord
    from poirot.backend.agents.multiagent.evolution.types import FailureCategory

    failure_category = getattr(result, "failure_category", None)
    if isinstance(failure_category, str):
        try:
            failure_category = FailureCategory(failure_category)
        except ValueError:
            failure_category = None

    record = DecisionLogRecord(
        log_id=str(uuid.uuid4()),
        specialist_name=specialist_name,
        task_id=str(uuid.uuid4()),
        goal=goal,
        success_criteria=success_criteria,
        failure_category=failure_category,
        success_criteria_met=1 if getattr(result, "success", False) else 0,
        lesson_text=None,  # MVP 不生成 lesson，L3 后置分析留 follow-up
        timestamp=utc_now_iso(),
    )
    writer.write_async(record)


def make_specialist_tool(
    name: str,
    specialist: SpecialistAgent,
    context_summarizer: ContextSummarizer,
    result_summarizer: ResultSummarizer,
    *,
    max_steps: int = 50,
    timeout_seconds: int = 600,
    version_dag: Any | None = None,
    budget_guard: Any | None = None,
    decision_log_writer: Any | None = None,
) -> BaseTool:
    """Factory：为 specialist 动态生成 delegate_to_<name> tool。

    tool handler 内部编排：ContextSummarizer → specialist.invoke →
    ResultSummarizer → JSON。LLM 只填 3 参数（goal + success_criteria +
    sandbox_id 可选），其余内部处理。

    - L2 扩展：budget_guard 非 None 时 check_and_record，超限返回 BudgetExceeded JSON。
    - L3 扩展：decision_log_writer 非 None 时异步写 DecisionLogRecord（fire-and-forget）。
    - version_dag 为预留参数，本函数未使用。

    Args:
        name: specialist 名，用于工具名 delegate_to_<name>。
        specialist: specialist 实例（SpecialistAgent 契约）。
        context_summarizer: 输入端摘要器。
        result_summarizer: 输出端摘要器。
        max_steps: 传给 specialist 的最大步数。
        timeout_seconds: 传给 specialist 的超时（秒）。
        version_dag: L2 版本 DAG（预留，未使用）。
        budget_guard: L2 预算守卫，非 None 时启用预算检查。
        decision_log_writer: L3 决策日志 writer，非 None 时异步写日志。

    Returns:
        名为 delegate_to_<name> 的 BaseTool。
    """

    @tool(f"delegate_to_{name}")
    def delegate_tool(
        goal: str,
        success_criteria: str,
        sandbox_id: str | None = None,
    ) -> str:
        """Delegate task to specialist. Provide goal and success_criteria. sandbox_id optional (uses thread sandbox if omitted)."""
        state = get_current_state()
        resolved_sandbox_id = _extract_sandbox_id(state, sandbox_id)

        # L2 预算检查：超限直接返回 BudgetExceeded JSON，不再调用 specialist
        if budget_guard is not None:
            from types import SimpleNamespace
            cost = SimpleNamespace(tokens=0, cost_usd=0.0, calls=1)
            budget_result = budget_guard.check_and_record(name, cost)
            if not budget_result.allowed:
                return json.dumps({
                    "success": False,
                    "error": {
                        "type": "BudgetExceeded",
                        "message": f"{name} budget exceeded: {budget_result.reason}",
                        "remaining": {
                            "tokens": budget_result.remaining.tokens if budget_result.remaining else 0,
                            "cost_usd": budget_result.remaining.cost_usd if budget_result.remaining else 0.0,
                            "calls": budget_result.remaining.calls if budget_result.remaining else 0,
                        },
                        "fallback_target": "lead",
                    },
                    "suggestion": f"{name} daily budget exceeded, lead agent should execute task directly or wait UTC 0 reset.",
                })

        context_summary = context_summarizer.summarize(state, goal, success_criteria)

        request = SpecialistRequest(
            goal=goal,
            success_criteria=success_criteria,
            context_summary=context_summary,
            sandbox_id=resolved_sandbox_id,
            artifacts_path=state.get("metadata", {}).get("artifacts_path"),
            max_steps=max_steps,
            timeout_seconds=timeout_seconds,
        )

        try:
            raw = specialist.invoke(request)
        except SpecialistError as e:
            return json.dumps({
                "success": False,
                "error": {
                    "type": type(e).__name__,
                    "message": str(e),
                },
                "suggestion": "retry, fallback to another specialist, or self-do",
            })

        result = result_summarizer.summarize(
            raw.raw_output,
            list(raw.artifacts),
            goal,
            success_criteria,
        )

        # L3 扩展：异步写 DecisionLogRecord（fire-and-forget，不阻塞 L1 turn）
        if decision_log_writer is not None:
            _write_decision_log_async(decision_log_writer, name, goal, success_criteria, result)

        return json.dumps({
            "success": result.success,
            "summary": result.summary,
            "specialist": result.specialist_name,
            "gap_analysis": result.gap_analysis,
            "artifacts": [
                {"path": a.path, "type": a.artifact_type}
                for a in result.artifacts
            ],
        })

    return delegate_tool


def make_subagent_tool(
    subagent_provider: SubagentProvider,
    context_summarizer: ContextSummarizer,
    result_summarizer: ResultSummarizer,
    *,
    max_steps: int = 20,
    timeout_seconds: int = 300,
) -> BaseTool:
    """Factory：生成 delegate_to_subagent tool（Poirot self-copy subagent）。

    - leaf role：子 agent 的 tool_groups 不含 multiagent，不能再 spawn。
    - shared thread sandbox：复用父 sandbox_id。

    Args:
        subagent_provider: 自复制 subagent 提供者。
        context_summarizer: 输入端摘要器。
        result_summarizer: 输出端摘要器。
        max_steps: 子 agent 最大步数。
        timeout_seconds: 子 agent 超时（秒）。

    Returns:
        名为 delegate_to_subagent 的 BaseTool。
    """

    @tool("delegate_to_subagent")
    def delegate_tool(
        goal: str,
        success_criteria: str,
        sandbox_id: str | None = None,
    ) -> str:
        """Delegate task to a Poirot self-copy subagent (leaf role, isolated context, shared sandbox). Provide goal and success_criteria."""
        state = get_current_state()
        resolved_sandbox_id = _extract_sandbox_id(state, sandbox_id)

        context_summary = context_summarizer.summarize(state, goal, success_criteria)

        request = SubagentRequest(
            goal=goal,
            success_criteria=success_criteria,
            context_summary=context_summary,
            sandbox_id=resolved_sandbox_id,
            artifacts_path=state.get("metadata", {}).get("artifacts_path"),
            max_steps=max_steps,
            timeout_seconds=timeout_seconds,
        )

        try:
            sub_result = subagent_provider.spawn(request)
        except SubagentError as e:
            return json.dumps({
                "success": False,
                "error": {
                    "type": type(e).__name__,
                    "message": str(e),
                },
                "suggestion": "retry, fallback to specialist, or self-do",
            })

        evaluated = result_summarizer.summarize(
            sub_result.summary,
            list(sub_result.artifacts),
            goal,
            success_criteria,
        )

        return json.dumps({
            "success": evaluated.success,
            "summary": evaluated.summary,
            "specialist": "subagent",
            "gap_analysis": evaluated.gap_analysis,
            "artifacts": [
                {"path": a.path, "type": a.artifact_type}
                for a in evaluated.artifacts
            ],
        })

    return delegate_tool