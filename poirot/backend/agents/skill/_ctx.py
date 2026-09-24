"""Skill provenance ContextVar — 跨 hook 桥接 active_skills + applied 标记。

【整体职责】
定义两个模块级 ContextVar，作为 skill 打点链路的「跨 hook 旁路通道」。
用于把「本轮注入了哪些 skill」与「哪些 skill 被实际应用」，
在缺少 state 参数的 hook 之间传递。

为什么需要：
- awrap_tool_call 没有 state 参数，无法读 state.metadata。
- 因此由 SkillInjectionMiddleware.before_model 写入 ContextVar，
  SkillMetricsMiddleware.awrap_tool_call 读 / 改，
  SkillMetricsMiddleware.after_agent 读回，完成归因打点。

【内容摘要】
- _active_skills_ctx ：本轮注入的 active skill 及其 allowed_tools。
- _applied_ctx       ：awrap_tool_call 标记的被实际应用的 tool-skill。

两个变量均为模块级 ContextVar，默认值 None；本模块只定义，不含读写逻辑。
读写分别发生在：
- skill_injection_middleware.py（写 _active_skills_ctx）
- skill_metrics_middleware.py（读 _active_skills_ctx、写/读 _applied_ctx）

【职责边界】
- 只负责：定义跨 hook 传递用的 ContextVar 容器。
- 不负责：写入、读取、判断逻辑（均在两个 middleware 内）。
- 不对外暴露：命名带下划线前缀，不进入 skill 包的 __all__。
- 独立成文件的原因：injection 与 metrics 两个 middleware 都要用这两个变量，
  若定义在其中一方会形成循环依赖，故抽到独立模块供双方安全 import。

【INVARIANT】
- _active_skills_ctx: [(skill_id, allowed_tools)]，本轮注入的 active skill。
- _applied_ctx: {skill_id: bool}，awrap_tool_call 标记的 applied
  （None 表示未标记 / guidance-skill）。
- 两者默认值均为 None。
- 未设（无 injection middleware）→ 均 None，SkillMetrics 降级（§6.6）。
- ContextVar 按执行上下文隔离，适配并发 / 多会话，避免全局变量串数据。
"""
from __future__ import annotations

from contextvars import ContextVar

# [(skill_id, allowed_tools)] — 本轮 active skill 及其声明工具。
# 写入：SkillInjectionMiddleware.before_model（有 state）。
# 读取：SkillMetricsMiddleware.awrap_tool_call（无 state，靠此旁路取得清单）。
_active_skills_ctx: ContextVar[list[tuple[str, tuple[str, ...]]] | None] = ContextVar(
    "poirot_skill_active", default=None,
)

# {skill_id: True} — awrap_tool_call 标记被实际应用的 tool-skill。
# 写入：SkillMetricsMiddleware.awrap_tool_call。
# 读取：SkillMetricsMiddleware.after_agent（有 state，回读后做归因打点）。
_applied_ctx: ContextVar[dict[str, bool] | None] = ContextVar(
    "poirot_skill_applied", default=None,
)