"""ToolCallMiddleware — 工具调用账本 + 失败分类 + 重试预算 + 超限禁工具 + 硬预算。

【整体职责】
作为最外层的 wrap_tool_call，能同时看到「工具最终结果」和「内层抛出的异常」，
因此在这里统一做四件事：
1. 账本：无论成功还是失败，都往 state.errors 里追加一条 AgentError
   （kind=success / failure），作为后续判定的数据来源。
2. 失败分类：把异常 / 业务失败归到 network / rate_limit / blocked / empty /
   server_error / client_error / unknown 等类型，并给出中文原因。
3. 重试预算：per-tool 连续失败达到 retry_budget 时，短路禁用该工具。
4. 硬预算：per-run 工具调用总数达到 hard_budget 时，短路禁用所有工具，
   强制模型基于现有证据收尾。

【内容摘要】
- 常量：_RETRY_BUDGET / _HARD_BUDGET / _SUMMARY_THRESHOLDS / _REASON_MAP。
- 正则：_BLOCKED_RE / _EMPTY_RE。
- 辅助函数：_make_id / _now_iso / _tool_text / _classify_exception /
  _classify_business_failure / _reason_for / _is_failure / _latest_attempt /
  _total_calls / _field。
- ToolCallMiddleware：账本 + 预算 + 重试（含 before_agent / before_model /
  wrap_tool_call / awrap_tool_call + 内部辅助）。

【职责边界】
- 只负责：工具调用账本、失败分类、重试预算、硬预算、失败摘要延迟注入。
- 不负责：工具的执行（handler）、熔断器（MCP 专属）、审计日志（McpAudit）、
  evidence 抽取（EvidenceMiddleware）。

【数据来源】
per-tool 连续失败次数、全局调用数，都从 state.errors 派生（不另存计数器）。
per-run 计数用 before_agent 记录的 baseline 做切片，避免跨 run 累积。

【启用范围】
仅 general / expert 模式启用。

【配对完整性】
异常或 handler 返回 None 时，会合成一条 ToolMessage 补上 tool_call_id，
保证 AIMessage(tool_calls) 后紧跟 ToolMessage，否则下一轮 model 调用会 400。

【延迟注入】
failure_summary / budget_exhausted 提示不在 wrap_tool_call 里直接注入
HumanMessage（那会插在并行 tool_calls 的多条 ToolMessage 之间，破坏配对），
而是先入队，等 before_model（此时 ToolMessage 已全部就位）再 drain 注入。

【INVARIANT】
- 成败都记账本：success / failure 各写一条 AgentError 到 state.errors。
- per-tool 连续失败用 attempt 字段派生（成功归 0）。
- per-run 计数用 baseline 切片（before_agent 记录）。
- 异常或 result=None 时合成 ToolMessage 补 tool_call_id（配对完整性）。
- 失败摘要入队延迟注入（避免破坏并行 tool_calls 配对）。
- 重试 / 硬预算短路时返回 Command（不调 handler）。
- 每个 hook 同步 / 异步版本行为一致（异步委托同步）。
"""
from __future__ import annotations

import re
import threading
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, override

from langchain.agents.middleware.types import AgentMiddleware, hook_config
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.runtime import Runtime
from langgraph.types import Command

from poirot.backend.agents.middlewares.run_journal_middleware import _get_runtime_value
from poirot.backend.agents.state.types import AgentError, ThreadState

_RETRY_BUDGET = 999       # per-tool 连续失败上限（达到即禁该工具）——放宽：用户要求取消限制
_HARD_BUDGET = 999        # run 级工具调用总数上限（达到即禁所有工具）——放宽：用户要求取消限制
_SUMMARY_THRESHOLDS = (3, 6, 9)  # 失败摘要递进注入阈值（连续失败到这些次数时注入）

# 业务失败特征正则（HTTP 200 但内容含这些特征时判为失败）
_BLOCKED_RE = re.compile(r"blocked|forbidden|captcha|access denied|403", re.IGNORECASE)
_EMPTY_RE = re.compile(r"no results? found|no results|empty result|nothing found|0 results", re.IGNORECASE)

# 错误类型 → 中文原因模板
_REASON_MAP = {
    "network": "网络问题（超时/连接失败）",
    "rate_limit": "API 限流",
    "blocked": "内容被封锁/拒绝访问",
    "empty": "无搜索结果",
    "server_error": "服务端错误（5xx）",
    "client_error": "客户端错误（4xx，换 provider 也会失败）",
    "unknown": "未知错误",
}


def _make_id(prefix: str) -> str:
    """生成带前缀的短 id（前缀 + 12 位 hex）。"""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _now_iso() -> str:
    """当前 UTC 时间，ISO 格式（秒级精度）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _tool_text(result: Any) -> str:
    """把工具结果压平成字符串（供分类 / 摘要使用）。

    - ToolMessage：按其 content 形态处理——
        · str  → 原样；
        · list → 逐项拼接（dict 含 "text" 取该字段，否则 str(item)），跳过假值项；
        · 其他 → str(content)。
    - 其他对象：str(result)。

    Args:
        result: 工具调用结果。

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


def _classify_exception(exc: Exception) -> str:
    """按异常类型归基本错误分类（F5）。

    判定顺序：
    1. TimeoutError / ConnectionError → "network"。
    2. 若 openai 可导入且异常为 RateLimitError → "rate_limit"；
       为 APIStatusError 时按 status_code：5xx → "server_error"，
       4xx → "client_error"。
    3. 兜底按异常文本关键词：
       - 含 timeout / timed out / connection → "network"；
       - 同时含 rate 与 limit → "rate_limit"。
    4. 都不命中 → "unknown"。

    Args:
        exc: 捕获到的异常。

    Returns:
        str: 错误类型字符串。
    """
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return "network"
    try:
        import openai
        if isinstance(exc, getattr(openai, "RateLimitError", type(None))):
            return "rate_limit"
        if isinstance(exc, getattr(openai, "APIStatusError", type(None))):
            status = getattr(exc, "status_code", None)
            if status is not None and 500 <= status < 600:
                return "server_error"
            if status is not None and 400 <= status < 500:
                return "client_error"
    except ImportError:
        pass
    msg = str(exc).lower()
    if "timeout" in msg or "timed out" in msg or "connection" in msg:
        return "network"
    if "rate" in msg and "limit" in msg:
        return "rate_limit"
    return "unknown"


def _classify_business_failure(text: str) -> str | None:
    """识别 HTTP 200 但内容含业务失败特征的情况（F5）。

    命中 _BLOCKED_RE → "blocked"；命中 _EMPTY_RE → "empty"；否则 None。

    Args:
        text: 工具结果的文本内容。

    Returns:
        str | None: 错误类型或 None。
    """
    if _BLOCKED_RE.search(text):
        return "blocked"
    if _EMPTY_RE.search(text):
        return "empty"
    return None


def _reason_for(error_type: str) -> str:
    """错误类型 → 中文原因模板（未命中返回 "未知错误"）。"""
    return _REASON_MAP.get(error_type, "未知错误")


def _is_failure(result: Any) -> tuple[str, str] | None:
    """判断工具结果是否失败。返回 (error_type, reason) 或 None。

    判定顺序：
    1. ToolMessage.status == "error"：
       从 content 文本里找错误类型关键词（network / rate_limit / blocked /
       empty / server_error / client_error），命中则返回对应类型；
       否则返回 ("unknown", 原因)。
    2. 业务失败特征（HTTP 200 但内容含封锁/空结果）→ 对应类型。
    3. 工具返回的错误 JSON（含 '"error"' 且文本里有 search failed /
       not installed / failed）→ ("unknown", "工具返回错误")。
    4. 都不命中 → None（视为成功）。

    Args:
        result: 工具调用结果。

    Returns:
        tuple[str, str] | None: (error_type, reason) 或 None。
    """
    if isinstance(result, ToolMessage) and getattr(result, "status", None) == "error":
        # 内层已标 error（如 Evidence 捕获异常）—— 从 content 推断分类
        text = _tool_text(result)
        for et in ("network", "rate_limit", "blocked", "empty", "server_error", "client_error"):
            if et in text.lower():
                return et, _reason_for(et)
        return "unknown", _reason_for("unknown")
    text = _tool_text(result)
    biz = _classify_business_failure(text)
    if biz:
        return biz, _reason_for(biz)
    # 检测工具返回的 error JSON（如 ddg {"error": "Search failed: ..."}）
    if '"error"' in text and ('search failed' in text.lower() or 'not installed' in text.lower() or 'failed' in text.lower()):
        return "unknown", "工具返回错误"
    return None


def _latest_attempt(errors: list, tool_name: str) -> int:
    """取该 tool 最新条目的 attempt（连续失败计数，成功条目 attempt=0）。

    从 errors 末尾往前找第一条 tool_name 匹配的条目，返回其 attempt
    （取不到字段时按 0）。找不到该 tool 的条目返回 0。

    Args:
        errors: AgentError 列表。
        tool_name: 工具名。

    Returns:
        int: 该工具的当前连续失败次数。
    """
    for err in reversed(errors):
        if _field(err, "tool_name") == tool_name:
            return int(_field(err, "attempt") or 0)
    return 0


def _total_calls(errors: list) -> int:
    """全局工具调用数 = len(errors)（成功+失败都记）。"""
    return len(errors)


def _field(item: Any, name: str) -> Any:
    """统一字段访问：dict 用 get，其他对象用 getattr（无则 None）。"""
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


class ToolCallMiddleware(AgentMiddleware):
    """工具调用账本：成败都记 errors，per-tool 重试/禁工具，硬预算兜底。

    failure_summary + budget_exhausted 提示用队列延迟到 before_model 注入，
    避免在 wrap_tool_call 注入 HumanMessage 插在并行 tool_calls 的 ToolMessage 之间
    破坏 API pairing（AIMessage(tool_calls) 后必须紧跟 ToolMessage）。

    Attributes:
        state_schema: 状态 schema（ThreadState）。
        _retry_budget: per-tool 连续失败上限。
        _hard_budget: per-run 工具调用总数上限。
        _lock: 保护队列与基线的锁。
        _pending_summaries: 待注入摘要队列（按 (thread_id, run_id) 分组）。
        _run_baselines: per-run 计数基线（errors 长度）。
    """

    state_schema = ThreadState  # type: ignore[assignment]

    def __init__(self, retry_budget: int = _RETRY_BUDGET, hard_budget: int = _HARD_BUDGET) -> None:
        """初始化。

        Args:
            retry_budget: per-tool 连续失败上限，达到即禁该工具。
            hard_budget: per-run 工具调用总数上限，达到即禁所有工具。
        """
        self._retry_budget = retry_budget
        self._hard_budget = hard_budget
        self._lock = threading.Lock()
        self._pending_summaries: dict[tuple[str, str], list[str]] = {}
        self._run_baselines: dict[tuple[str, str], int] = {}

    def _queue_key(self, runtime: Runtime) -> tuple[str, str]:
        """当前 (thread_id, run_id) 作为队列 / 基线的键（缺省 "default"）。"""
        tid = str(_get_runtime_value(runtime, "thread_id", None) or "default")
        rid = str(_get_runtime_value(runtime, "run_id", None) or "default")
        return (tid, rid)

    def _queue_summary(self, runtime: Runtime, text: str) -> None:
        """把一条待注入摘要入队（按 queue_key 分组，加锁）。"""
        key = self._queue_key(runtime)
        with self._lock:
            self._pending_summaries.setdefault(key, []).append(text)

    def _drain_summaries(self, runtime: Runtime) -> list[str]:
        """取出并清空当前 key 的所有待注入摘要。"""
        key = self._queue_key(runtime)
        with self._lock:
            return self._pending_summaries.pop(key, None) or []

    def _set_baseline(self, runtime: Runtime, count: int) -> None:
        """记录当前 run 起始时 errors 的长度，作为 per-run 计数基线。"""
        key = self._queue_key(runtime)
        with self._lock:
            self._run_baselines[key] = count

    def _run_tool_count(self, runtime: Runtime, errors: list) -> int:
        """per-run 工具调用数 = len(errors) - baseline（baseline 在 before_agent 记录）。"""
        key = self._queue_key(runtime)
        with self._lock:
            baseline = self._run_baselines.get(key, 0)
        return max(0, len(errors) - baseline)

    def _run_errors_slice(self, runtime: Runtime, errors: list) -> list:
        """返回当前 run 的 errors 切片（baseline 之后），用于 per-run retry budget。"""
        key = self._queue_key(runtime)
        with self._lock:
            baseline = self._run_baselines.get(key, 0)
        return errors[baseline:] if baseline > 0 else errors

    def _journal(self, runtime: Runtime) -> Any:
        """从 runtime 取 journal（无则 None）。"""
        return _get_runtime_value(runtime, "journal", None)

    def _emit(self, runtime: Runtime, event_type: str, payload: dict) -> None:
        """向 journal 追加事件（无 journal 时静默跳过）。"""
        j = self._journal(runtime)
        if j is not None:
            j.append(event_type, payload)

    @override
    def before_agent(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        """before_agent：记录 errors 基线 + 清理其他 run 的陈旧队列 / 基线。

        处理流程：
        1. 取 state.errors 的长度，作为本次 run 的 per-run 计数基线。
        2. 清理同一个 thread 下、run_id 不等于当前 run 的 pending_summaries
           与 run_baselines（避免旧 run 残留污染）。

        Args:
            state: 当前 state，读取 errors。
            runtime: LangGraph 运行时，读取 thread_id / run_id。

        Returns:
            dict[str, Any] | None: 始终 None（只做基线记录与清理，不改 state）。
        """
        # 记录 errors 基线（per-run 工具调用计数基准）
        errors = state.get("errors") if isinstance(state, dict) else None
        self._set_baseline(runtime, len(errors or []))
        # 清理其他 run 的陈旧队列 + 基线
        tid = str(_get_runtime_value(runtime, "thread_id", None) or "default")
        rid = str(_get_runtime_value(runtime, "run_id", None) or "default")
        with self._lock:
            stale = [k for k in list(self._pending_summaries) if k[0] == tid and k[1] != rid]
            for k in stale:
                self._pending_summaries.pop(k, None)
                self._run_baselines.pop(k, None)
        return None

    def _build_failure_summary(self, errors: list, tool_name: str) -> str:
        """构造结构化失败摘要（错误类型 → 原因模板）。

        取该 tool 的所有 failure 条目，输出：
        - 首行：连续失败次数；
        - 末 3 条：每条 "- {error_type}: {reason or message}"；
        - 尾行：行动建议（换搜索词/换工具/换方法，或基于现有证据收尾）。

        Args:
            errors: 当前 errors 列表（含刚追加的这条）。
            tool_name: 工具名。

        Returns:
            str: 多行摘要文本。
        """
        fails = [e for e in errors if _field(e, "tool_name") == tool_name and _field(e, "kind") == "failure"]
        lines = [f"工具 {tool_name} 已连续失败 {len(fails)} 次："]
        for e in fails[-3:]:
            lines.append(f"- {_field(e, 'error_type')}: {_field(e, 'reason') or _field(e, 'message')}")
        lines.append("请分析失败模式，考虑换搜索词/换工具/换方法；若判断不可恢复，基于现有证据收尾报告。")
        return "\n".join(lines)

    def _process_result(
        self, request: ToolCallRequest, result: Any, runtime: Runtime, exc: Exception | None,
    ) -> Any:
        """返回阶段处理：记 errors 账本 + 失败摘要 + 硬预算。

        处理流程：
        1. 取 tool_name / call_id / state.errors；用 _run_errors_slice 取
           当前 run 的 errors 切片。
        2. 判定成败：
           - exc 非空：按 _classify_exception 分类；attempt = 该 tool 上次
             attempt + 1；写 AgentError(kind="failure")；emit
             tool.failure_streak；合成 error ToolMessage 补 tool_call_id。
           - exc 为空：用 _is_failure 判断——
               · 失败：分类 + attempt + 写 failure AgentError + emit；
               · 成功：写 success AgentError（attempt=0）。
        3. 组装 update = {"errors": [err]}；run_count = per-run 计数 + 1。
        4. 若 result 为 None（handler 返 None 无异常）：补空 ToolMessage
           保 tool_call 配对完整。
        5. 若失败且 attempt ∈ _SUMMARY_THRESHOLDS（3/6/9）：_build_failure_summary
           入队（延迟到 before_model 注入）。
        6. 若 run_count >= hard_budget：入队提示 + emit tool.budget_exhausted。
        7. 合并返回：
           - result 是 Command：把 errors 合并进 result.update 后返回新 Command；
           - 否则：update["messages"] = [result]，返回 Command(update=update)。

        Args:
            request: 工具调用请求（含 tool_call / state / runtime）。
            result: handler 的返回值（异常路径传 None）。
            runtime: LangGraph 运行时。
            exc: 捕获到的异常；无异常传 None。

        Returns:
            Command（带 errors 与 messages 更新）。
        """
        tool_name = request.tool_call.get("name", "")
        call_id = request.tool_call.get("id", "")
        state = request.state
        errors = state.get("errors") or [] if isinstance(state, dict) else []
        # per-run errors slice 用于 retry budget 判定
        run_errors = self._run_errors_slice(runtime, errors)

        # 判定成败 + 分类
        if exc is not None:
            error_type = _classify_exception(exc)
            kind = "failure"
            attempt = _latest_attempt(run_errors, tool_name) + 1
            reason = _reason_for(error_type)
            err = AgentError(
                error_id=_make_id("err"), stage="tool",
                message=f"{tool_name}: {exc}", tool_name=tool_name,
                kind=kind, attempt=attempt, error_type=error_type, reason=reason,
                related_refs=(call_id,), created_at=_now_iso(),
            )
            self._emit(runtime, "tool.failure_streak", {"tool": tool_name, "attempt": attempt, "type": error_type})
            # 合成 error ToolMessage 补 tool_call_id，保证 tool_call/tool_response 配对完整
            # （缺则下一轮 model 调用 400: insufficient tool messages following tool_calls）
            result = ToolMessage(
                content=f"⚠️ 工具 {tool_name} 执行异常（{error_type}）：{exc}",
                tool_call_id=call_id,
                status="error",
            )
        else:
            fail = _is_failure(result)
            if fail:
                error_type, reason = fail
                kind = "failure"
                attempt = _latest_attempt(run_errors, tool_name) + 1
                err = AgentError(
                    error_id=_make_id("err"), stage="tool",
                    message=f"{tool_name}: 业务失败 {reason}", tool_name=tool_name,
                    kind=kind, attempt=attempt, error_type=error_type, reason=reason,
                    related_refs=(call_id,), created_at=_now_iso(),
                )
                self._emit(runtime, "tool.failure_streak", {"tool": tool_name, "attempt": attempt, "type": error_type})
            else:
                kind = "success"
                attempt = 0
                err = AgentError(
                    error_id=_make_id("err"), stage="tool",
                    message=f"{tool_name}: success", tool_name=tool_name,
                    kind=kind, attempt=attempt, error_type="", reason="",
                    related_refs=(call_id,), created_at=_now_iso(),
                )

        # 记账本——per-run 计数
        run_count = self._run_tool_count(runtime, errors) + 1
        update: dict[str, Any] = {"errors": [err]}

        # 守卫：result 为 None（handler 返 None 无异常）—— 补空 ToolMessage 保 tool_call 配对完整
        if result is None:
            result = ToolMessage(content="", tool_call_id=call_id)

        # F8.2：失败摘要递进注入（3/6/9）—— 队列延迟到 before_model，避免插在并行 ToolMessage 间破坏 pairing
        if kind == "failure" and attempt in _SUMMARY_THRESHOLDS:
            summary = self._build_failure_summary(errors + [err], tool_name)
            self._queue_summary(request.runtime, summary)

        # F8.5：硬预算兜底 —— per-run
        if run_count >= self._hard_budget:
            self._queue_summary(
                runtime,
                f"本轮工具调用总数已达 {run_count}（预算 {_HARD_BUDGET}），必须收尾报告。",
            )
            self._emit(runtime, "tool.budget_exhausted", {"count": run_count, "max": self._hard_budget})

        # 合并：若 result 已是 Command（内层 Evidence 返回），合并 errors 进 update
        if isinstance(result, Command):
            merged = dict(result.update)
            merged.setdefault("errors", []).extend(update["errors"])
            return Command(update=merged)
        # 非 Command（原 ToolMessage 或异常）：返 Command 带 errors + result ToolMessage
        update["messages"] = [result] if result is not None else []
        return Command(update=update)

    @hook_config(can_jump_to=["model"])
    @override
    def before_model(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        """drain 队列的 failure_summary / budget_exhausted 提示，在 before_model 注入。

        before_model 在 ToolNode 之后执行（ToolMessage 已全部就位），
        HumanMessage 注入在 ToolMessage 之后不破坏 AIMessage(tool_calls)→ToolMessage pairing。

        Args:
            state: 当前 state（本 hook 未直接使用）。
            runtime: LangGraph 运行时，用于取队列。

        Returns:
            dict[str, Any] | None: 含注入 HumanMessage 的 state patch；
                队列为空时返回 None。
        """
        summaries = self._drain_summaries(runtime)
        if not summaries:
            return None
        combined = "\n\n".join(dict.fromkeys(summaries))
        return {"messages": [HumanMessage(
            name="tool_failure_summary",
            additional_kwargs={"hide_from_ui": True},
            content=f"<system_reminder>\n{combined}\n</system_reminder>",
        )]}

    @hook_config(can_jump_to=["model"])
    @override
    async def abefore_model(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        """异步 before_model：直接转调同步版，保证行为一致。"""
        return self.before_model(state, runtime)

    @override
    def wrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        """同步 wrap_tool_call：预算短路 → handler → 结果处理。

        处理流程：
        1. 取 tool_name / state.errors；算 run_count 与 run_errors（当前 run 切片）。
        2. 禁工具短路：若该 tool 的最近 attempt >= retry_budget：
           emit tool.blocked + 返回带 error ToolMessage 的 Command（不调 handler）。
        3. 硬预算短路：若 run_count >= hard_budget：
           emit tool.budget_exhausted + 返回带 error ToolMessage 的 Command。
        4. 正常路径：handler(request)——
           - 成功：_process_result(..., exc=None)；
           - 异常：_process_result(..., exc=exc)。

        Args:
            request: 工具调用请求。
            handler: 下游处理函数。

        Returns:
            Command（带 messages / errors 更新）。
        """
        tool_name = request.tool_call.get("name", "")
        state = request.state
        errors = state.get("errors") or [] if isinstance(state, dict) else []
        run_count = self._run_tool_count(request.runtime, errors)
        # per-run errors slice：只看当前 run 的 errors（baseline 后），防跨 run 持久禁工具
        run_errors = self._run_errors_slice(request.runtime, errors)

        # F8.3：禁工具短路——per-run retry budget
        if _latest_attempt(run_errors, tool_name) >= self._retry_budget:
            self._emit(request.runtime, "tool.blocked", {"tool": tool_name, "reason": "retry_budget_exhausted"})
            failure_msg = ToolMessage(
                content=f"⚠️ 工具 {tool_name} 已达重试上限（{self._retry_budget}），已被禁用，请换方法或收尾。",
                tool_call_id=request.tool_call.get("id", ""),
                status="error",
            )
            return Command(update={"messages": [failure_msg]})

        # F8.5：硬预算短路——per-run 调用数达上限，拒绝所有后续工具，强制模型收尾
        if run_count >= self._hard_budget:
            self._emit(request.runtime, "tool.budget_exhausted", {"count": run_count, "max": self._hard_budget})
            failure_msg = ToolMessage(
                content=f"⚠️ 本轮工具调用已达预算上限（{self._hard_budget}），所有工具已禁用，请立即基于现有证据输出最终报告。",
                tool_call_id=request.tool_call.get("id", ""),
                status="error",
            )
            return Command(update={"messages": [failure_msg]})

        try:
            result = handler(request)
            return self._process_result(request, result, request.runtime, exc=None)
        except Exception as exc:
            return self._process_result(request, None, request.runtime, exc=exc)

    @override
    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """异步 wrap_tool_call：逻辑与同步版一致，仅 handler 改为 await。

        处理流程与 wrap_tool_call 相同：
        1. 取 tool_name / errors / run_count / run_errors。
        2. retry_budget 短路 → Command；
        3. hard_budget 短路 → Command；
        4. await handler(request) → _process_result（成功 / 异常）。

        Args:
            request: 工具调用请求。
            handler: 下游异步处理函数。

        Returns:
            Command（带 messages / errors 更新）。
        """
        tool_name = request.tool_call.get("name", "")
        state = request.state
        errors = state.get("errors") or [] if isinstance(state, dict) else []
        run_count = self._run_tool_count(request.runtime, errors)
        run_errors = self._run_errors_slice(request.runtime, errors)

        if _latest_attempt(run_errors, tool_name) >= self._retry_budget:
            self._emit(request.runtime, "tool.blocked", {"tool": tool_name, "reason": "retry_budget_exhausted"})
            failure_msg = ToolMessage(
                content=f"⚠️ 工具 {tool_name} 已达重试上限（{self._retry_budget}），已被禁用，请换方法或收尾。",
                tool_call_id=request.tool_call.get("id", ""),
                status="error",
            )
            return Command(update={"messages": [failure_msg]})

        # F8.5：硬预算短路——per-run
        if run_count >= self._hard_budget:
            self._emit(request.tool.runtime, "tool.budget_exhausted", {"count": run_count, "max": self._hard_budget})
            failure_msg = ToolMessage(
                content=f"⚠️ 本轮工具调用已达预算上限（{self._hard_budget}），所有工具已禁用，请立即基于现有证据输出最终报告。",
                tool_call_id=request.tool_call.get("id", ""),
                status="error",
            )
            return Command(update={"messages": [failure_msg]})

        try:
            result = await handler(request)
            return self._process_result(request, result, request.runtime, exc=None)
        except Exception as exc:
            return self._process_result(request, None, request.runtime, exc=exc)