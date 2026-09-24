"""标签化上下文格式基座 — TaggedContextMiddleware + ContextAssembler。

【整体职责】
把送往 LLM 的上下文渲染成「扁平 XML 标签序列 + <turn> 时序分组」。
渲染是 request-scoped 的：只在本次模型调用内生效，不落盘、不持久化。
state 中始终保存原始 message content 与 additional_kwargs 标记，
渲染只是「读取并改写」，不回写原始数据。

【内容摘要】
- 标记常量（层 2 命名空间 poirot.*）：POIROT_THINKING / POIROT_SUMMARY /
  POIROT_EXTERNALIZED / POIROT_EXTERNALIZED_PATH / POIROT_EXTERNALIZED_META /
  POIROT_COMPACTION_STAGE / POIROT_TURN_ID。
- _field                     : 统一从 dict / dataclass / 对象取字段。
- ContextAssembler           : 渲染扁平 XML 标签序列（request-scoped）。
- ContextAssembler.render_context_block : 头部上下文块（goal/plan/reflection/summary/date）。
- ContextAssembler.render_messages      : message 序列 → <turn> 分组纯字符串（trace）。
- ContextAssembler.render_messages_for_llm : 改写后的 messages 列表（送 LLM）。
- TaggedContextMiddleware    : wrap_model_call 组装 + after_model 写 trace。

【职责边界】
- 只负责：把 state 字段与 message 序列渲染成送 LLM 的 messages（request-scoped）。
- 不负责：state 字段的写入（各中间件）、压缩/摘要逻辑（StrategyMiddleware）、
  消息规范化（MessageNormalizerMiddleware）、持久化（checkpointer）。

【INVARIANT】
- 两层标记体系：层 1 = 送 LLM 的 XML 标签；层 2 = additional_kwargs 的 poirot.* 命名空间。
- request-scoped：渲染只影响本次模型调用，不回写 state 原始数据。
- 双渲染路径：render_messages（trace 审计）+ render_messages_for_llm（真送 LLM）。
- 方案 H：保留 message 角色，AIMessage content 包语义标签，ToolMessage 保持经典形态。
- SystemMessage 跳过：其内容由 wrap_model_call 提取进 <system> 标签。
- <observations> 舍弃：原始数据仍在 state，供 ReflectionMiddleware 使用。
- 日期始终渲染：<date> 无条件输出（东八区）。
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, override

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.runtime import Runtime

from poirot.backend.agents.state.types import ThreadState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 层 2 标记键名（additional_kwargs 命名空间 poirot.*）
#
# 作用：压缩筛选 + 幂等 + trace 审计。
# 统一使用 poirot.* 前缀做命名空间隔离，防止与其他 middleware 的
# additional_kwargs 键发生冲突。
# ---------------------------------------------------------------------------
POIROT_THINKING = "poirot.thinking"                      # 标记该 AIMessage 携带思维链
POIROT_SUMMARY = "poirot.summary"                        # 标记该 HumanMessage 是摘要消息
POIROT_EXTERNALIZED = "poirot.externalized"              # 标记该 ToolMessage 结果已外置
POIROT_EXTERNALIZED_PATH = "poirot.externalized_path"    # 外置结果的存储路径
POIROT_EXTERNALIZED_META = "poirot.externalized_meta"    # 外置结果的元数据（如节省 token 数）
POIROT_COMPACTION_STAGE = "poirot.compaction_stage"      # 压缩阶段标记
POIROT_TURN_ID = "poirot.turn_id"                        # turn 标识

_CST = timezone(timedelta(hours=8))  # 东八区，用于 <date> 与 trace 时间戳


def _field(item: Any, name: str) -> Any:
    """从 dict / dataclass / 普通对象中统一取字段。

    渲染 plan、reflection 等结构时，元素可能是 dict，也可能是 dataclass
    或普通对象。本函数抹平差异：
    - dict        → item.get(name)
    - 其他对象    → getattr(item, name, None)，取不到返回 None

    Args:
        item: 待取字段的元素。
        name: 字段名。

    Returns:
        Any: 字段值；不存在时返回 None。
    """
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


class ContextAssembler:
    """渲染扁平 XML 标签序列。request-scoped，不持久。

    职责划分：
    - render_context_block：渲染头部上下文块，把 state 字段映射为
      <goal><plan><reflection><summary><date>。<system> 不在这里渲染，
      而是在 TaggedContextMiddleware.wrap_model_call 中从 request 的
      SystemMessage 提取后拼接。
    - render_messages：把 message 序列渲染为 <turn> 时序分组的纯字符串，
      供 trace 审计使用。
    - render_messages_for_llm：返回改写后的 messages 列表，供真正送 LLM 使用。

    注意：<observations> 被舍弃（Q5 决策），原始数据仍留在 state 中，
    供 ReflectionMiddleware 使用。

    Attributes:
        _max_reflections: <reflection> 块最多渲染的反思项数。
    """

    def __init__(self, max_reflections: int = 10) -> None:
        """初始化。

        Args:
            max_reflections: <reflection> 块最多渲染最近多少条反思项，
                防止上下文膨胀。
        """
        self._max_reflections = max_reflections

    def render_context_block(self, state: Mapping[str, Any], governance: dict | None) -> str:
        """渲染头部上下文块（state 字段 + governance → XML 标签串）。

        渲染顺序与对应来源：
        - <goal>       ← state["research_question"]
        - <plan>       ← state["todos"]
        - <reflection> ← state["reflection_items"]
        - <summary>    ← governance["default"]["summary"]
        - <date>       ← 当前东八区日期，始终渲染

        空值字段会被跳过，不产生空标签。

        Args:
            state: ThreadState 映射。
            governance: 治理配置字典，可为 None。

        Returns:
            str: 以换行连接的 XML 标签字符串。
        """
        lines: list[str] = []

        goal = state.get("research_question") or ""
        if goal:
            lines.append(f"<goal>{goal}</goal>")

        plan = self._render_plan(state.get("todos"))
        if plan:
            lines.append(f"<plan>\n{plan}\n</plan>")

        reflection = self._render_reflection(state)
        if reflection:
            lines.append(f"<reflection>\n{reflection}\n</reflection>")

        summary = self._render_summary(governance)
        if summary:
            lines.append(f"<summary>\n{summary}\n</summary>")

        lines.append(f"<date>{self._current_date()}</date>")
        return "\n".join(lines)

    def _render_plan(self, todos: list | None) -> str:
        """把 todos 渲染成勾选清单文本。

        每条 todo 输出一行 `[mark] title`：
        - mark 取值：completed → "x"，in_progress → ">"，其余 → 空格。
        - 标题优先取 title，其次 description，最后退化为 str(t)。

        Args:
            todos: todo 列表，可为 None 或空。

        Returns:
            str: 多行文本；无内容时返回空串。
        """
        if not todos:
            return ""
        items: list[str] = []
        for t in todos:
            title = _field(t, "title") or _field(t, "description") or str(t)
            status = _field(t, "status") or ""
            mark = {"completed": "x", "in_progress": ">"}.get(status, " ")
            items.append(f"[{mark}] {title}")
        return "\n".join(items)

    def _render_summary(self, governance: dict | None) -> str:
        """从 governance 中提取摘要文本。

        读取路径：governance["default"]["summary"]。
        任一层缺失都安全返回空串。

        Args:
            governance: 治理配置字典，可为 None。

        Returns:
            str: 摘要字符串；无内容时返回空串。
        """
        if not governance:
            return ""
        default = governance.get("default") or {}
        return default.get("summary") or ""

    def _render_reflection(self, state: Mapping[str, Any]) -> str:
        """渲染反思项列表，只取最近 max_reflections 条。

        每条输出一行：`- [status] scope/kind: question`。
        字段缺失时用兜底值：scope/kind 为空串，question 退化为 str(item)，
        status 默认 "open"。

        Args:
            state: ThreadState 映射。

        Returns:
            str: 多行文本；无反思项时返回空串。
        """
        items = state.get("reflection_items") or []
        if not items:
            return ""
        recent = list(items)[-self._max_reflections:]
        lines: list[str] = []
        for item in recent:
            scope = _field(item, "scope") or ""
            kind = _field(item, "kind") or ""
            question = _field(item, "question") or str(item)
            status = _field(item, "status") or "open"
            lines.append(f"- [{status}] {scope}/{kind}: {question}")
        return "\n".join(lines)

    def _current_date(self) -> str:
        """返回当前东八区日期，格式 `YYYY-MM-DD, Weekday`。"""
        return datetime.now(_CST).strftime("%Y-%m-%d, %A")

    def render_messages(self, messages: list) -> str:
        """渲染 message 序列为 <turn> 时序分组的纯字符串（trace 审计用）。

        渲染规则：
        - SystemMessage：跳过（<system> 由 wrap_model_call 从 request 提取）。
        - HumanMessage 且带 poirot.summary：跳过（<summary> 已在上下文块渲染）。
        - HumanMessage 普通：关闭上一个 <turn>，开启新 <turn id=N>，
          并输出 <message role="user">。
        - AIMessage：
            · 带 poirot.thinking → 输出 <thinking>（内容取 additional_kwargs
              的 reasoning_content）。
            · tool_calls → 每个输出一个自闭合 <toolcall name=... args=.../>，
              args 截断到 100 字符。
            · content 非空 → 输出 <answer>。
        - ToolMessage：
            · 带 poirot.externalized → 输出带 path / tokens 属性的
              <toolresult>，内容为外置后的文本。
            · 普通 → 输出 <toolresult name=...>。
        - 遍历结束后，若仍有未闭合的 <turn>，补上 </turn>。

        Args:
            messages: message 列表。

        Returns:
            str: 以换行连接的 XML 字符串。
        """
        lines: list[str] = []
        turn_id = 0
        turn_open = False

        for msg in messages:
            if isinstance(msg, SystemMessage):
                continue
            if isinstance(msg, HumanMessage) and msg.additional_kwargs.get(POIROT_SUMMARY):
                continue

            if isinstance(msg, HumanMessage):
                if turn_open:
                    lines.append("</turn>")
                turn_id += 1
                lines.append(f'<turn id="{turn_id}">')
                turn_open = True
                lines.append(f'<message role="user">{self._escape(self._extract_text(msg))}</message>')
                continue

            if isinstance(msg, AIMessage):
                if msg.additional_kwargs.get(POIROT_THINKING):
                    thinking = msg.additional_kwargs.get("reasoning_content") or ""
                    if thinking:
                        lines.append(f"<thinking>{self._escape(thinking)}</thinking>")
                for tc in msg.tool_calls or []:
                    name = tc.get("name", "") if isinstance(tc, dict) else ""
                    args = str(tc.get("args", "") if isinstance(tc, dict) else "")[:100]
                    lines.append(f'<toolcall name="{self._escape(name)}" args="{self._escape(args)}"/>')
                content = self._extract_text(msg)
                if content:
                    lines.append(f"<answer>{self._escape(content)}</answer>")
                continue

            if isinstance(msg, ToolMessage):
                name = msg.name or "unknown"
                content = self._extract_text(msg)
                if msg.additional_kwargs.get(POIROT_EXTERNALIZED):
                    path = msg.additional_kwargs.get(POIROT_EXTERNALIZED_PATH, "")
                    meta = msg.additional_kwargs.get(POIROT_EXTERNALIZED_META) or {}
                    tokens = meta.get("tokens_saved", "")
                    lines.append(
                        f'<toolresult name="{self._escape(name)}" path="{self._escape(path)}" tokens="{tokens}">'
                        f"{self._escape(content)}</toolresult>"
                    )
                else:
                    lines.append(f'<toolresult name="{self._escape(name)}">{self._escape(content)}</toolresult>')
                continue

        if turn_open:
            lines.append("</turn>")
        return "\n".join(lines)

    @staticmethod
    def _extract_text(message: Any) -> str:
        """从各种形态的 message 中提取纯文本。

        支持的输入：
        - str：直接返回。
        - 带 content 属性的对象（如 BaseMessage）：取 content。
        - dict：取 "content" 键。
        - content 为 list：拼接其中 str 元素与 dict 元素的 "text" 字段。

        Args:
            message: 待提取的对象。

        Returns:
            str: 提取到的纯文本；content 为 None 时返回空串。
        """
        if isinstance(message, str):
            return message
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict):
                    parts.append(part.get("text", ""))
            return "".join(parts)
        return str(content) if content is not None else ""

    @staticmethod
    def _escape(text: str) -> str:
        """转义 XML 特殊字符，防止用户内容破坏标签结构。

        顺序很重要：先转义 &，再转义 < 和 >，否则会二次转义。

        Args:
            text: 原始文本。

        Returns:
            str: 转义后的文本。
        """
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def render_messages_for_llm(self, messages: list) -> list:
        """返回改写后的 messages 列表，供真正送 LLM 使用（方案 H）。

        方案 H 的核心：保留 message 角色（不把历史压成单串），只对
        AIMessage 的 content 包上语义标签，ToolMessage 保持经典形态。

        逐条处理规则：
        - SystemMessage：跳过（其内容由 wrap_model_call 提取进上下文块 <system>）。
        - HumanMessage 且带 poirot.summary：跳过（<summary> 已在上下文块）。
        - HumanMessage 普通：原样保留。
        - AIMessage：交给 _rewrite_ai_message 改写 content。
        - 其他类型：原样保留。

        Args:
            messages: 原始 message 列表。

        Returns:
            list: 改写后的 message 列表。
        """
        rewritten: list = []
        for msg in messages:
            if isinstance(msg, SystemMessage):
                continue
            if isinstance(msg, HumanMessage):
                if msg.additional_kwargs.get(POIROT_SUMMARY):
                    continue
                rewritten.append(msg)
                continue
            if isinstance(msg, AIMessage):
                rewritten.append(self._rewrite_ai_message(msg))
                continue
            rewritten.append(msg)
        return rewritten

    def _rewrite_ai_message(self, msg: AIMessage) -> AIMessage:
        """把 AIMessage 的 content 改写为 <thinking>...</thinking><answer>...</answer>。

        仅改 content 文本，机制层的 tool_calls 等字段保持不变。
        - 若带 poirot.thinking，则把 reasoning_content 包进 <thinking>。
        - content 非空则包进 <answer>。
        - 若两者都没有内容，则退回原始 content，避免产生空 content。

        Args:
            msg: 原始 AIMessage。

        Returns:
            AIMessage: 改写后的 AIMessage（model_copy，不原地修改）。
        """
        parts: list[str] = []
        if msg.additional_kwargs.get(POIROT_THINKING):
            thinking = msg.additional_kwargs.get("reasoning_content") or ""
            if thinking:
                parts.append(f"<thinking>{self._escape(thinking)}</thinking>")
        content = self._extract_text(msg)
        if content:
            parts.append(f"<answer>{self._escape(content)}</answer>")
        new_content = "".join(parts) if parts else msg.content
        return msg.model_copy(update={"content": new_content})


class TaggedContextMiddleware(AgentMiddleware):
    """标签化上下文基座 middleware。

    在 wrap_model_call 中调用 ContextAssembler 渲染扁平标签序列，
    通过 request.override 替换送 LLM 的 messages。渲染是 request-scoped，
    不持久化（state 始终保存原始 message content）。

    trace 审计通过 logger 记录；state.tagged_context 字段预留接口，
    后续可补持久快照。

    state_schema 绑定 ThreadState，使 middleware 能读取
    research_question / todos / reflection_items / governance 等字段。

    Attributes:
        _assembler: ContextAssembler 实例。
    """

    state_schema = ThreadState  # type: ignore[assignment]

    def __init__(self, assembler: ContextAssembler | None = None) -> None:
        """初始化。

        Args:
            assembler: 自定义渲染器；为 None 时使用默认 ContextAssembler。
        """
        self._assembler = assembler or ContextAssembler()

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        """同步模型调用入口：先组装上下文，再交给下游 handler。

        Args:
            request: 原始模型调用请求。
            handler: 下游处理函数。

        Returns:
            ModelCallResult: handler 返回的模型调用结果。
        """
        assembled = self._assemble(request)
        logger.info("tagged_context assembled: %d messages", len(assembled.messages))
        return handler(assembled)

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        """异步模型调用入口：先组装上下文，再 await 下游 handler。

        Args:
            request: 原始模型调用请求。
            handler: 下游异步处理函数。

        Returns:
            ModelCallResult: 下游返回的模型调用结果。
        """
        assembled = self._assemble(request)
        logger.info("tagged_context assembled: %d messages", len(assembled.messages))
        return await handler(assembled)

    def _assemble(self, request: ModelRequest) -> ModelRequest:
        """组装送 LLM 的 messages（方案 H）。

        步骤：
        1. 从 request.runtime.state 取 state，从 request.messages 取原始消息。
        2. 从原始消息中提取 SystemMessage 文本。
        3. 用 ContextAssembler 渲染头部上下文块。
        4. 若有 system 文本，以 <system> 标签置于上下文块最前。
        5. 用 render_messages_for_llm 改写对话历史（保留角色）。
        6. 把上下文块包成一条 SystemMessage 放在最前，后接改写后的历史。
        7. 通过 request.override 返回新请求。

        Args:
            request: 原始模型调用请求。

        Returns:
            ModelRequest: 替换了 messages 的新 ModelRequest。
        """
        state = getattr(getattr(request, "runtime", None), "state", None) or {}
        messages = getattr(request, "messages", None) or []
        system_text = self._extract_system(messages)
        governance = state.get("governance")
        context_block = self._assembler.render_context_block(state, governance)
        if system_text:
            context_block = f"<system>\n{system_text}\n</system>\n\n{context_block}"
        rewritten = self._assembler.render_messages_for_llm(messages)
        new_messages: list = []
        if context_block:
            new_messages.append(SystemMessage(content=context_block))
        new_messages.extend(rewritten)
        return request.override(messages=new_messages)

    @override
    def after_model(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:
        """模型调用后钩子：生成 trace 审计快照写入 state.tagged_context。

        与 _assemble 不同，这里从 state.messages 重新渲染一份完整快照，
        仅用于审计，不影响实际送 LLM 的内容。

        步骤：
        1. 取 state.messages 与 state.governance。
        2. 提取 system 文本并拼接 <system> 到上下文块。
        3. 用 render_messages 渲染 message 序列（纯字符串，含 <turn> 分组）。
        4. 合并上下文块与消息渲染结果，附上 created_at 时间戳。

        Args:
            state: ThreadState。
            runtime: LangGraph 运行时（此处未使用）。

        Returns:
            dict[str, Any] | None: 含 tagged_context 字段的 dict，供 state 更新。
        """
        messages = state.get("messages") or []
        governance = state.get("governance")
        system_text = self._extract_system(messages)
        context_block = self._assembler.render_context_block(state, governance)
        if system_text:
            context_block = f"<system>\n{system_text}\n</system>\n\n{context_block}"
        messages_trace = self._assembler.render_messages(messages)
        trace = (context_block + "\n\n" + messages_trace) if context_block else messages_trace
        return {"tagged_context": {"rendered": trace, "created_at": datetime.now(_CST).isoformat()}}

    @staticmethod
    def _extract_system(messages: list) -> str:
        """从 message 列表中提取第一条 SystemMessage 的文本内容。

        用于在 wrap_model_call / after_model 中把系统提示词放进 <system> 标签。
        只取第一条；content 非 str 时强转为 str。

        Args:
            messages: message 列表。

        Returns:
            str: SystemMessage 的文本；不存在时返回空串。
        """
        for msg in messages:
            if isinstance(msg, SystemMessage):
                content = msg.content
                return content if isinstance(content, str) else str(content)
        return ""