"""PoirotStreamClient — 流式研究服务（仿 deer-flow DeerFlowClient.stream）。

【整体职责】
用 graph.astream(stream_mode=["values", "messages", "custom"]) 消费 LangGraph 原生流，
把 token 级 delta（messages）、完整状态快照（values）、自定义事件（custom）标准化为
StreamEvent 供 CLI 渲染。流式消费归本 service，不在 LeaderAgent 内（LeaderAgent 只管 run()）。

【内容摘要】
- _StreamEventBase    : 流式事件标准化结构。
- StreamEvent         : 事件类型 + 可选 budget 字段。
- _extract_text       : 从 message content 提取纯文本（兼容 str / list[dict]）。
- _extract_reasoning  : 提取 reasoning_content delta（thinking token）。
- _truncate           : 截断文本。
- _is_skills_selector_output : 检测 SkillSelector 的 JSON 输出。
- _strip_skills_leak  : 渲染层兜底，剥离残留的 SkillSelector JSON。
- PoirotStreamClient  : 流式客户端，stream(question) 产出 StreamEvent。

【职责边界】
- 只负责：消费 graph.astream、标准化 StreamEvent、跨模式去重、done/error 检测。
- 不负责：渲染（stream_handler）、图执行与决策（leader/agent + middleware）、
  run 生命周期（run_manager）、budget 的展示（status_bar）。

【INVARIANT】
- 三模式消费：values（状态快照）/ messages（token delta）/ custom（middleware 发的事件）。
- 双模式去重：seen_ids / streamed_ids / seen_tool_call_ids 防止重复输出。
- 内部 LLM 过滤：metadata.tags 含 "internal_llm" 的 chunk 跳过，防泄漏到 CLI。
- selector 输出拦截：按 msg_id 累积缓冲，命中 _is_skills_selector_output 则丢弃；
  非 {/` 开头或超 200 字符则确认非 selector 并 flush。
- 首帧 values 预填：第一帧含 checkpoint 恢复的旧 messages，预填 seen_ids 跳过。
- budget 零值过滤：window == 0 视为 init_budget 零快照，跳过防视觉闪烁。
- astream 结束 yield done：无精确 done 信号，靠结束事件。
- 残留 buffer flush：流结束后 flush 未决 answer buffer（跳过 selector 与已 streamed）。
"""
from __future__ import annotations

from typing import Any, AsyncIterator, TypedDict

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage


class _StreamEventBase(TypedDict):
    """流式事件标准化结构，供 CLI 消费渲染。

    Attributes:
        type: 事件类型（thinking / answer / tool_start / tool_end / skill_active /
            done / error / budget_update / sandbox_update / help_requested /
            activity_started / activity_finished / activity_heartbeat）。
        content: 事件文本内容。
        tool_name: 工具名。
        tool_args: 工具参数。
        tool_result: 工具结果。
        msg_id: 消息 ID。
    """

    type: str
    content: str
    tool_name: str | None
    tool_args: dict | None
    tool_result: str | None
    msg_id: str | None


class StreamEvent(_StreamEventBase, total=False):
    """budget_update 事件携带的上下文预算快照（{total, fraction, window}）。

    其他事件类型不设置此字段——消费者用 ``event.get("budget")`` 取值，缺省时返回 None。
    """

    budget: dict | None


def _extract_text(content: Any) -> str:
    """从 message content 提取纯文本（兼容 str / list[dict]）。

    Args:
        content: 消息内容。

    Returns:
        str: 提取出的文本。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return str(content) if content else ""


def _extract_reasoning(chunk: AIMessageChunk) -> str:
    """提取 reasoning_content delta（deepseek 等 thinking token）。

    Args:
        chunk: AIMessageChunk。

    Returns:
        str: reasoning_content 文本；无则空串。
    """
    additional = getattr(chunk, "additional_kwargs", {}) or {}
    return additional.get("reasoning_content", "") or ""


def _truncate(text: str, limit: int = 200) -> str:
    """截断文本至 limit 字符，超出加省略号。

    Args:
        text: 原文本。
        limit: 长度上限。

    Returns:
        str: 截断后的文本。
    """
    return text[:limit] + "..." if len(text) > limit else text


def _is_skills_selector_output(text: str) -> bool:
    """检测 SkillSelector LLM 输出：{"skills": [...]} 可能带 markdown fence 或前缀文字。

    SkillSelector._llm_select 用 llm.invoke 选 skill，返回 JSON。同步 invoke 的
    internal_llm tag 在 async 流式 metadata 中可能丢失，此函数作为 content fallback：
    提取首个 JSON 对象，解析成功且含 "skills" key 即判定为 selector 输出。

    Args:
        text: 累积文本。

    Returns:
        bool: True 表示是 selector 输出。
    """
    if not text or '"skills"' not in text:
        return False
    import re
    cleaned = re.sub(r'^```(?:json)?\s*\n?', '', text.strip())
    cleaned = re.sub(r'\n?```\s*$', '', cleaned)
    s = cleaned.find('{')
    e = cleaned.rfind('}')
    if s == -1 or e == -1 or e <= s:
        return False
    try:
        import json
        data = json.loads(cleaned[s:e + 1])
        return isinstance(data, dict) and 'skills' in data
    except (ValueError, TypeError):
        return False


def _strip_skills_leak(text: str) -> str:
    """渲染层兜底：从 answer 文本中剥离残留的 SkillSelector JSON 输出。

    stream 层按 msg_id 累积检测能拦截大部分情况（纯 JSON / markdown fence），
    但 LLM 在 JSON 前加解释文字时首个 delta 不以 { 开头会直接 yield，此函数
    在渲染前清洗 full_answer：移除 {"skills":[...]} JSON 片段及其 markdown 包裹。

    Args:
        text: 待清洗文本。

    Returns:
        str: 清洗后的文本。
    """
    if not text or '"skills"' not in text:
        return text
    import re
    # 移除 markdown code block 包裹的 skills JSON
    text = re.sub(r'```(?:json)?\s*\n?\{"skills"\s*:.*?\}\s*\n?```\s*', '', text, flags=re.DOTALL)
    # 移除裸 skills JSON（可能带前缀文字）
    text = re.sub(r'\{"skills"\s*:\s*\[.*?\]\}\s*', '', text, flags=re.DOTALL)
    return text.strip()


class PoirotStreamClient:
    """流式研究服务——消费 graph.astream，产出 StreamEvent。

    仿 deer-flow DeerFlowClient.stream()：
    - stream_mode=["values", "messages"] 双模式
    - messages mode 拿 token delta（thinking/answer/tool）
    - values mode 去重 + 检测完成
    - seen_ids / streamed_ids 跨模式去重

    Attributes:
        _graph: 编译后的 LangGraph graph。
        _config: graph 调用配置。
    """

    def __init__(self, graph: Any, config: dict) -> None:
        """初始化。

        Args:
            graph: 编译后的 graph。
            config: 调用配置。
        """
        self._graph = graph
        self._config = config

    async def stream(self, question: str) -> AsyncIterator[StreamEvent]:
        """流式产出 StreamEvent。

        Args:
            question: 用户研究问题。

        Yields:
            StreamEvent: thinking / answer / tool_start / tool_end / done / error 等。
        """
        from langchain_core.messages import HumanMessage

        state = {
            "messages": [HumanMessage(content=question)],
            "user_input": question,
            "research_question": question,
        }

        seen_ids: set[str] = set()
        streamed_ids: set[str] = set()
        seen_tool_call_ids: set[str] = set()
        _internal_answer_ids: set[str] = set()  # sync invoke 内部 LLM 响应（tag 未传播时 fallback）
        _answer_buffers: dict[str, str] = {}    # msg_id → 累积 answer 文本（selector 检测缓冲）
        _answer_safe: set[str] = set()          # msg_id 确认非 selector 输出（直接 yield）
        first_values_frame = True

        async for item in self._graph.astream(
            state,
            config=self._config,
            stream_mode=["values", "messages", "custom"],
        ):
            # 多 mode 时 astream 产 (mode, chunk) tuple
            if isinstance(item, tuple) and len(item) == 2:
                mode, chunk = item
                mode = str(mode)
            else:
                mode, chunk = "values", item

            # custom mode: middleware/strategy 用 get_stream_writer() 发的 custom event
            # （如 DefaultStrategy compaction_start/progress/end）
            if mode == "custom":
                if isinstance(chunk, dict) and "type" in chunk:
                    yield StreamEvent(
                        type=chunk["type"],
                        content=chunk.get("content", ""),
                        tool_name=chunk.get("tool_name"),
                        tool_args=chunk.get("tool_args"),
                        tool_result=chunk.get("tool_result"),
                        msg_id=chunk.get("msg_id"),
                        **({"skills": chunk.get("skills", [])} if chunk.get("skills") is not None else {}),
                    )
                continue

            if mode == "messages":
                # messages mode: (message_chunk, metadata) tuple
                if isinstance(chunk, tuple) and len(chunk) == 2:
                    msg_chunk, _metadata = chunk
                else:
                    msg_chunk = chunk
                    _metadata = {}

                # 过滤内部 LLM 调用（summarizer/reporter/reflection/skill-selector）——tag=internal_llm
                # 防止压缩/报告/反思/skill选择的 model.invoke 输出泄漏到 CLI 当作 answer 渲染
                if isinstance(_metadata, dict):
                    _tags = _metadata.get("tags") or []
                    if "internal_llm" in _tags:
                        continue

                msg_id = getattr(msg_chunk, "id", None)

                # AIMessageChunk → thinking + answer + tool_calls
                if isinstance(msg_chunk, AIMessageChunk) or isinstance(msg_chunk, AIMessage):
                    # thinking: reasoning_content delta
                    reasoning = _extract_reasoning(msg_chunk)
                    if reasoning:
                        if msg_id:
                            streamed_ids.add(msg_id)
                        yield StreamEvent(
                            type="thinking", content=reasoning,
                            tool_name=None, tool_args=None, tool_result=None, msg_id=msg_id,
                        )

                    # answer: content delta — 按 msg_id 累积检测 selector 输出
                    # SkillSelector 的 llm.invoke 返回 {"skills":[...]} JSON，sync invoke 的
                    # internal_llm tag 在 async 流中可能丢失。JSON 跨多个 token delta，
                    # 逐 delta 检测无法命中，必须累积后判断。
                    text = _extract_text(msg_chunk.content)
                    if not text:
                        pass  # 空 delta，跳过下面处理
                    elif msg_id and msg_id in _internal_answer_ids:
                        pass  # 已确认 selector 输出，丢弃
                    elif msg_id and msg_id in _answer_safe:
                        # 已确认非 selector，直接 yield
                        streamed_ids.add(msg_id)
                        yield StreamEvent(
                            type="answer", content=text,
                            tool_name=None, tool_args=None, tool_result=None, msg_id=msg_id,
                        )
                    else:
                        # 新 msg_id 或仍在缓冲 — 累积后判断
                        buf = _answer_buffers.get(msg_id, "") + text
                        _answer_buffers[msg_id] = buf

                        if _is_skills_selector_output(buf):
                            # 确认 selector 输出，丢弃整个 buffer
                            _internal_answer_ids.add(msg_id)
                            _answer_buffers.pop(msg_id, None)
                        else:
                            stripped = buf.lstrip()
                            first = stripped[:1] if stripped else ""
                            # selector 输出必以 { 或 ` (markdown fence) 开头
                            # 非 {/` 开头 → 确认非 selector，flush buffer
                            if first and first not in ('{', '`'):
                                _answer_safe.add(msg_id)
                                _answer_buffers.pop(msg_id, None)
                                streamed_ids.add(msg_id)
                                yield StreamEvent(
                                    type="answer", content=buf,
                                    tool_name=None, tool_args=None, tool_result=None, msg_id=msg_id,
                                )
                            elif len(buf) > 200:
                                # 超 selector 输出长度上限 → 确认非 selector，flush
                                _answer_safe.add(msg_id)
                                _answer_buffers.pop(msg_id, None)
                                streamed_ids.add(msg_id)
                                yield StreamEvent(
                                    type="answer", content=buf,
                                    tool_name=None, tool_args=None, tool_result=None, msg_id=msg_id,
                                )
                            # else: 仍以 {/` 开头且 < 200 字符，继续缓冲等下个 delta

                    # tool_calls → tool_start（流式增量 chunk 会为同一 tool_call 重复产出——
                    # 首个 delta 通常带完整 name，后续只带 args 增量、name 常为空——按 id 去重
                    # + 跳过空 name，否则渲染层会看到好几行 "unknown"）
                    tool_calls = getattr(msg_chunk, "tool_calls", None) or []
                    for tc in tool_calls:
                        if isinstance(tc, dict):
                            tc_name = tc.get("name", "")
                            tc_args = tc.get("args", {})
                            tc_id = tc.get("id", "")
                            if not tc_name or (tc_id and tc_id in seen_tool_call_ids):
                                continue
                            if tc_id:
                                seen_tool_call_ids.add(tc_id)
                            yield StreamEvent(
                                type="tool_start", content="",
                                tool_name=tc_name,
                                tool_args=tc_args if isinstance(tc_args, dict) else None,
                                tool_result=None, msg_id=tc_id,
                            )

                # ToolMessage → tool_end
                elif isinstance(msg_chunk, ToolMessage):
                    if msg_id:
                        streamed_ids.add(msg_id)
                    result_text = _extract_text(msg_chunk.content)
                    yield StreamEvent(
                        type="tool_end", content="",
                        tool_name=getattr(msg_chunk, "name", "") or "",
                        tool_args=None,
                        tool_result=_truncate(result_text),
                        msg_id=msg_id,
                    )

                continue

            # mode == "values": 完整状态快照——去重 + done 检测
            if mode == "values" and isinstance(chunk, dict):
                # budget_update：从 governance.default.budget 提取上下文占用快照
                # governance/budget 缺失时不 yield（None 保护，不报错）
                budget = (
                    chunk.get("governance", {})
                    .get("default", {})
                    .get("budget", {})
                )
                # 过滤 init_budget 的"全零"快照：DefaultStrategy.before_agent 会把
                # budget 重置成 {total:0, fraction:0.0, window:0}，下一帧 values 把这
                # 个零状态 yield 出去会让 TUI/CLI 的占用率显示先掉到 0.0% 再恢复——
                # 视觉闪烁。track() 跑过后 window 恒 > 0（resolve_window_size 兜底
                # 128000），fraction 也恒 > 0（messages 非空时 token_counter > 0），
                # 所以"window == 0"是 init_budget 独有签名，直接跳过即可。
                if (
                    budget
                    and budget.get("total") is not None
                    and budget.get("fraction") is not None
                    and (budget.get("window") or 0) > 0
                ):
                    yield StreamEvent(
                        type="budget_update",
                        content="",
                        tool_name=None,
                        tool_args=None,
                        tool_result=None,
                        msg_id=None,
                        budget={
                            "total": budget.get("current", budget.get("total", 0)),
                            "fraction": budget.get("fraction", 0.0),
                            "window": budget.get("window", 0),
                        },
                    )

                # sandbox_update：从 state.sandbox 提取 sandbox_id
                sandbox_state = chunk.get("sandbox")
                if sandbox_state and sandbox_state.get("sandbox_id"):
                    yield StreamEvent(
                        type="sandbox_update",
                        content=sandbox_state["sandbox_id"],
                        tool_name=None,
                        tool_args=None,
                        tool_result=None,
                        msg_id=None,
                    )

                messages = chunk.get("messages", [])
                # 第一帧 values 含 checkpoint 恢复的旧 messages，预填 seen_ids 跳过，防重复输出
                if first_values_frame:
                    for msg in messages:
                        msg_id = getattr(msg, "id", None)
                        if msg_id:
                            seen_ids.add(msg_id)
                            streamed_ids.add(msg_id)
                    first_values_frame = False
                    continue
                for msg in messages:
                    msg_id = getattr(msg, "id", None)
                    if msg_id and msg_id in seen_ids:
                        continue
                    if msg_id:
                        seen_ids.add(msg_id)

                    # 已通过 messages mode 流式输出了，跳过
                    if msg_id and msg_id in streamed_ids:
                        continue

                    # 未通过 messages mode 输出的消息（如非 streaming 模型）
                    if isinstance(msg, AIMessage):
                        text = _extract_text(msg.content)
                        if text and not _is_skills_selector_output(text):
                            yield StreamEvent(
                                type="answer", content=text,
                                tool_name=None, tool_args=None, tool_result=None, msg_id=msg_id,
                            )
                        tool_calls = getattr(msg, "tool_calls", None) or []
                        for tc in tool_calls:
                            if isinstance(tc, dict):
                                tc_name = tc.get("name", "")
                                tc_id = tc.get("id", "")
                                if not tc_name or (tc_id and tc_id in seen_tool_call_ids):
                                    continue
                                if tc_id:
                                    seen_tool_call_ids.add(tc_id)
                                yield StreamEvent(
                                    type="tool_start", content="",
                                    tool_name=tc_name,
                                    tool_args=tc.get("args") if isinstance(tc.get("args"), dict) else None,
                                    tool_result=None, msg_id=tc_id,
                                )
                    elif isinstance(msg, ToolMessage):
                        result_text = _extract_text(msg.content)
                        yield StreamEvent(
                            type="tool_end", content="",
                            tool_name=getattr(msg, "name", "") or "",
                            tool_args=None,
                            tool_result=_truncate(result_text),
                            msg_id=msg_id,
                        )

                # 检测 done：values 最后一帧含完整状态
                # LangGraph astream values mode 最后一帧是完整 final state
                # 无精确 "done" 信号——靠 astream 结束后 yield done
                continue

        # flush 残留 answer buffer（selector 检测未决的 msg_id）
        for mid, buf in _answer_buffers.items():
            if mid in _internal_answer_ids or mid in streamed_ids:
                continue
            if not _is_skills_selector_output(buf):
                streamed_ids.add(mid)
                yield StreamEvent(
                    type="answer", content=buf,
                    tool_name=None, tool_args=None, tool_result=None, msg_id=mid,
                )

        # astream 结束 → done
        yield StreamEvent(
            type="done", content="",
            tool_name=None, tool_args=None, tool_result=None, msg_id=None,
        )