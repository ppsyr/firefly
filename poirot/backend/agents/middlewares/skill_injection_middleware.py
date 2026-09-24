"""SkillInjectionMiddleware — before_model 注入 active skills 内容 + selections 打点 + provenance。

【整体职责】
在每次进入 model 之前，决定本次任务要激活哪些 skill，然后：
1. 把这些 skill 的说明内容作为一条 SystemMessage 注入 messages；
2. 对每个激活的 skill 做「选择打点」（store.record_selection + journal）；
3. 写 provenance 锚点，供下游中间件判断 skill 是否真正被应用。

【内容摘要】
- SkillInjectionMiddleware.__init__     : 接收 store（打点）与 selector（自动选择）。
- SkillInjectionMiddleware.before_model : 选 skill → 注入 + 打点 + 写 provenance。
- SkillInjectionMiddleware.abefore_model: 委托同步版（行为一致）。

【职责边界】
- 只负责：选 skill、注入 SystemMessage、selection 打点、写 provenance 锚点。
- 不负责：skill 的实际执行（由模型读注入内容自行决定）、是否被应用的判定
  （SkillMetricsMiddleware）、skill 的存储与检索（skill 子系统）、
  skill 内容渲染格式（injector 模块）。

【INVARIANT（必须保持的不变量）】
- 混合注入：激活来源有两路——
    · 用户 override（metadata.skill_override，或 /skill 命令经主循环注入的
      runtime configurable）；
    · agent 自动 select（由 Selector 根据 user_input 选出）。
  两路合并后再决定注入内容。
- before_model 负责：注入 SystemMessage + record_selection + journal skill.select。
- provenance 锚点分两处：
    · metadata.active_skills   ← 本次激活的 skill id 列表；
    · metadata.skill_applied   ← {id: None}，初始为 None，
                                 由 SkillMetricsMiddleware.awrap_tool_call
                                 在工具真正被调用后标 True。
- 无 active skills 时返回 None（不注入、不打点）。
- 无 store / journal 时静默不报错（INV#13，容忍运行环境缺件）。
- abefore_model 直接委托同步版，保证同步/异步行为一致。

【ContextVar provenance】
_active_skills_ctx / _applied_ctx 是 request-scoped 的 ContextVar，
供 SkillMetricsMiddleware.awrap_tool_call 读取 allowed_tools 并标记 applied。
"""
from __future__ import annotations

from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import SystemMessage
from langgraph.runtime import Runtime

from poirot.backend.agents.middlewares.run_journal_middleware import _get_runtime_value
from poirot.backend.agents.skill._ctx import _active_skills_ctx, _applied_ctx
from poirot.backend.agents.skill.injector import build_injection_text


class SkillInjectionMiddleware(AgentMiddleware):
    """before_model 注入 active skills 内容 + selections 打点 + provenance。

    持有一个 store（记录选择）与一个 selector（自动选择 skill）。
    真正的 store 优先从 runtime 取（skill_store），取不到再用构造时注入的。

    Attributes:
        _store: 构造时注入的 skill 存储（打点用）。
        _selector: skill 选择器。
    """

    def __init__(self, store: Any, selector: Any) -> None:
        """初始化。

        Args:
            store: skill 存储，用于 record_selection 打点。
            selector: skill 选择器，提供 select_for_task(user_input, overrides)。
        """
        self._store = store
        self._selector = selector

    def before_model(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        """同步 before_model hook：选 skill → 注入 + 打点 + 写 provenance。

        处理流程：
        1. 解析依赖：skill_store（runtime 优先，回退 self._store）、
           journal、run_id。
        2. 取 user_input（state["user_input"]，缺省空串）。
        3. 解析 overrides：
           - 优先 runtime configurable 的 "skill_override"（/skill 命令经主循环注入）；
           - 取不到再回退 state.metadata["skill_override"]（程序注入兜底）。
        4. 调 selector.select_for_task(user_input, overrides) 选出 active skills；
           若为空则返回 None（不注入、不打点）。
        5. 逐个 skill 打点：
           - store.record_selection(skill_id)，异常吞掉；
           - journal.append("skill.select", {...})，异常吞掉；
           - 收集 id 进 ids。
        6. 用 build_injection_text(active) 生成注入文本。
        7. 尽力通过 get_stream_writer 发一条 {"type": "skill_active", ...}
           流事件（异常吞掉，不影响主流程）。
        8. 写 request-scoped ContextVar：
           - _active_skills_ctx ← [(skill_id, allowed_tools), ...]；
           - _applied_ctx      ← {skill_id: None}。
        9. 返回 state patch：
           - messages += [SystemMessage(injection)]；
           - metadata.active_skills / skill_names / skill_applied。

        Args:
            state: 当前 state，读取 user_input 与 metadata.skill_override。
            runtime: LangGraph 运行时，读取 skill_store / journal / run_id /
                skill_override。

        Returns:
            dict[str, Any] | None: 含 messages 与 metadata 的 state patch；
                无 active skill 时返回 None。
        """
        store = _get_runtime_value(runtime, "skill_store") or self._store
        journal = _get_runtime_value(runtime, "journal", None)
        run_id = _get_runtime_value(runtime, "run_id", None)

        user_input = state.get("user_input", "") or ""
        # override：configurable（/skill 命令经主循环注入）优先，state.metadata 程序注入兜底
        overrides = _get_runtime_value(runtime, "skill_override")
        if not overrides:
            overrides = (state.get("metadata") or {}).get("skill_override") or []

        active = self._selector.select_for_task(user_input, overrides=overrides)
        if not active:
            return None

        ids: list[str] = []
        for rec in active:
            try:
                store.record_selection(rec.skill_id)
            except Exception:
                pass
            if journal is not None:
                try:
                    journal.append("skill.select", {
                        "skill_id": rec.skill_id,
                        "name": rec.name,
                        "run_id": run_id,
                    })
                except Exception:
                    pass
            ids.append(rec.skill_id)

        injection = build_injection_text(active)
        try:
            from langgraph.config import get_stream_writer
            get_stream_writer()({
                "type": "skill_active",
                "skills": [rec.name for rec in active],
            })
        except Exception:
            pass
        # provenance ContextVar：供 SkillMetricsMiddleware.awrap_tool_call 读 allowed_tools + 标 applied
        _active_skills_ctx.set([(rec.skill_id, rec.allowed_tools) for rec in active])
        _applied_ctx.set({sid: None for sid in ids})
        return {
            "messages": [SystemMessage(content=injection)],
            "metadata": {
                "active_skills": ids,
                "skill_names": [rec.name for rec in active],
                "skill_applied": {sid: None for sid in ids},
            },
        }

    async def abefore_model(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        """异步 before_model：直接委托同步版，保证行为一致。

        Args:
            state: 当前 state。
            runtime: LangGraph 运行时。

        Returns:
            dict[str, Any] | None: 同 before_model。
        """
        return self.before_model(state, runtime)