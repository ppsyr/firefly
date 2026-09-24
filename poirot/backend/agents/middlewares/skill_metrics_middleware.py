"""SkillMetricsMiddleware — applied 打点 + completions/fallbacks 归因。

【整体职责】
承接 SkillInjectionMiddleware 写下的 provenance 锚点，完成 skill 效果的
「应用打点」与「结果归因」：
1. awrap_tool_call：工具真正被调用时，判断它属于哪个 active skill 的
   allowed_tools，命中就把该 skill 标为 applied=True，并写 journal。
2. after_agent：在 run 结束时，对每个 active skill 做归因
   （record_outcome），并判定本次 run 是否任务完成。

【内容摘要】
- SkillMetricsMiddleware.__init__       : 接收 store（归因用）。
- SkillMetricsMiddleware.awrap_tool_call: 命中 allowed_tools → 标 applied + 写 journal。
- SkillMetricsMiddleware.after_agent    : run 级 task_completed 判定 + 对 active skill 归因。
- SkillMetricsMiddleware.aafter_agent   : 委托同步版（行为一致）。
- SkillMetricsMiddleware._judge_task_completed : run 级完成判定（近似代理信号）。
- _is_success_error                     : 判断 errors 条目是否属 success 类。

【职责边界】
- 只负责：applied 打点、completions/fallbacks 归因、run 级任务完成判定。
- 不负责：选 skill 与注入（SkillInjectionMiddleware）、skill 的实际执行、
  skill 存储实现（skill 子系统）、最终质量评估（L3 评估层）。

【INVARIANT（必须保持的不变量）】
- awrap_tool_call：
    · 若 tool_name ∈ 某个 active skill 的 allowed_tools，
      则 _applied_ctx[sid] = True，并 journal.append("skill.apply", ...)。
- after_agent：
    · 先做 run 级 task_completed 判定（与单个 skill 正交，不属于某个 skill）。
    · 再对每个 active skill 调 store.record_outcome 做归因：
        - tool-skill（有 allowed_tools）未命中工具 → applied=False
          （有能力用但没用）。
        - guidance-skill（无 allowed_tools）→ applied=None（不强判）。
        - applied=None → 只算「选中」，不归因 completion / fallback（INV#9）。
- provenance 通过 _ctx 里的 ContextVar 桥接：
    awrap_tool_call 拿不到 state，只能靠 ContextVar 读 provenance。
- 无 _applied_ctx（即没有 injection middleware 写入）→ 降级处理：
    after_agent 跳过归因（§6.6）。
- 无 store / journal 时静默不报错（INV#13，容忍运行环境缺件）。
- aafter_agent 直接委托同步版，保证同步/异步行为一致。
- 本方法只负责「标记 + 打点」，不修改工具调用的请求或结果。
- task_completed 是近似代理信号，非 ground-truth（INV#8）。
"""
from __future__ import annotations

from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langgraph.runtime import Runtime

from poirot.backend.agents.middlewares.run_journal_middleware import _get_runtime_value
from poirot.backend.agents.skill._ctx import _active_skills_ctx, _applied_ctx


class SkillMetricsMiddleware(AgentMiddleware):
    """applied + completions/fallbacks 打点。provenance 贯穿。

    持有一个 store（用于 record_outcome 归因）。真正的 store 优先从
    runtime 取（skill_store），取不到再用构造时注入的。

    Attributes:
        _store: 构造时注入的 skill 存储（归因用）。
    """

    def __init__(self, store: Any) -> None:
        """初始化。

        Args:
            store: skill 存储，用于 get / record_outcome 归因。
        """
        self._store = store

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        """异步 wrap_tool_call：命中 allowed_tools 时标 applied + 写 journal。

        因为 awrap_tool_call 拿不到 state，只能通过 ContextVar 读取
        SkillInjectionMiddleware 写下的 provenance。

        处理流程：
        1. 从 request 取 tool_call 与 tool_name（非 dict 时 tool_name 为空）。
        2. 取 runtime（用于读 journal）。
        3. 读 ContextVar：
           - active_list  ← _active_skills_ctx（[(skill_id, allowed_tools), ...]）
           - applied_map  ← _applied_ctx（{skill_id: None/True}）
        4. 若 active_list 非空、applied_map 不为 None、tool_name 非空：
           - 读 journal（可为 None）；
           - 遍历 active_list，若 tool_name ∈ allowed_tools：
               · applied_map[sid] = True；
               · journal 存在则 journal.append("skill.apply", {...})，异常吞掉；
           - 把更新后的 applied_map 写回 _applied_ctx。
        5. await handler(request) 并返回其结果。

        注意：本方法只负责「标记 + 打点」，不修改工具调用的请求或结果。

        Args:
            request: 工具调用请求（ToolCallRequest）。
            handler: 下游异步处理函数。

        Returns:
            Any: handler 返回的工具调用结果。
        """
        tool_call = getattr(request, "tool_call", None) or {}
        tool_name = tool_call.get("name", "") if isinstance(tool_call, dict) else ""
        runtime = getattr(request, "runtime", None)

        active_list = _active_skills_ctx.get()
        applied_map = _applied_ctx.get()
        if active_list and applied_map is not None and tool_name:
            journal = _get_runtime_value(runtime, "journal", None)
            for sid, allowed_tools in active_list:
                if tool_name in allowed_tools:
                    applied_map[sid] = True
                    if journal is not None:
                        try:
                            journal.append("skill.apply", {
                                "skill_id": sid, "tool_name": tool_name,
                            })
                        except Exception:
                            pass
            _applied_ctx.set(applied_map)

        return await handler(request)

    def after_agent(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        """同步 after_agent：run 结束时判 task_completed + 对 active skill 归因。

        处理流程：
        1. 解析 store：runtime.skill_store 优先，回退 self._store；
           若 store 为 None 直接返回 None（无 store 无法归因）。
        2. 取 run_id。
        3. 从 state.metadata 取 active_ids；无 active skill 直接返回 None。
        4. 读 ContextVar applied_map（可能为 None，表示降级：无 injection）。
        5. 调 _judge_task_completed(state) 得到 run 级 task_completed。
        6. 遍历 active_ids，对每个 sid：
           - store.get(sid) 取记录，异常吞掉得到 rec=None；
           - applied = applied_map.get(sid)（applied_map 为 None 时 applied=None）；
           - 若 rec 存在且 rec.allowed_tools 非空但 applied 仍为 None
             → applied=False（tool-skill 有机会用但没用）；
           - guidance-skill（无 allowed_tools）保持 applied=None；
           - store.record_outcome(sid, run_id, applied, task_completed)，
             异常吞掉。
        7. 返回 None（本 hook 不写 state patch）。

        Args:
            state: 当前 state，读取 metadata.active_skills 与 errors / final_report。
            runtime: LangGraph 运行时，读取 skill_store / run_id。

        Returns:
            dict[str, Any] | None: 始终返回 None（仅做打点与归因，不改 state）。
        """
        store = _get_runtime_value(runtime, "skill_store") or self._store
        if store is None:
            return None
        run_id = _get_runtime_value(runtime, "run_id", None)

        metadata = state.get("metadata") or {}
        active_ids = metadata.get("active_skills") or []
        if not active_ids:
            return None

        applied_map = _applied_ctx.get()
        task_completed = self._judge_task_completed(state)

        for sid in active_ids:
            try:
                rec = store.get(sid)
            except Exception:
                rec = None
            applied = applied_map.get(sid) if applied_map else None
            # tool-skill 有 allowed_tools 但未命中 → False（有机会用没用）
            if rec and rec.allowed_tools and applied is None:
                applied = False
            # guidance-skill（无 allowed_tools）→ applied 保持 None
            try:
                store.record_outcome(sid, run_id, applied, task_completed)
            except Exception:
                pass
        return None

    async def aafter_agent(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        """异步 after_agent：直接委托同步版，保证行为一致。

        Args:
            state: 当前 state。
            runtime: LangGraph 运行时。

        Returns:
            dict[str, Any] | None: 同 after_agent（始终 None）。
        """
        return self.after_agent(state, runtime)

    @staticmethod
    def _judge_task_completed(state: Any) -> bool:
        """run 级任务完成判定（与单个 skill 正交）。

        这是一个近似代理信号，非 ground-truth（INV#8）。

        判定规则：
        - run 模式（有 final_report）：final_report 已生成且无硬失败 → True。
        - chat 模式（无 final_report）：只看无硬失败（更宽松）。
        - 两种模式当前都等价于「无硬失败」，只是保留分支语义便于后续演进。

        硬失败的判定：errors 里过滤掉 success 类错误后仍有剩余。

        Args:
            state: 当前 state，读取 errors 与 final_report。

        Returns:
            bool: 任务是否完成。
        """
        errors = state.get("errors") or []
        hard_failures = [e for e in errors if not _is_success_error(e)]
        if state.get("final_report"):
            return len(hard_failures) == 0
        return len(hard_failures) == 0


def _is_success_error(e: Any) -> bool:
    """判断 errors 条目是否为 success 类（即非硬失败）。

    兼容两种形态：
    - dict：取 "kind" 字段比较；
    - AgentError dataclass / 普通对象：取 kind 属性比较。

    Args:
        e: errors 列表中的单个条目。

    Returns:
        bool: 该条目是否属于 success 类错误。
    """
    if isinstance(e, dict):
        return e.get("kind") == "success"
    return getattr(e, "kind", None) == "success"