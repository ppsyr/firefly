"""MemoryMiddleware — before_model 召回注入 + after_model 清除（Phase 1）。

【整体职责】
在每次模型调用前，从长期记忆召回与当前 query 相关的内容，以 per-call
HumanMessage 注入上下文（保护 prompt caching），并写 recalled_memories 索引；
在模型调用后清除 turn_id（traceability 生命周期）。

【内容摘要】
- _CHARS_PER_TOKEN           : token 估算常量（1 token ≈ 4 字符）。
- MemoryMiddleware.__init__   : 接收 provider 与召回/抽取开关、token 预算。
- MemoryMiddleware.abefore_model : 召回 + 注入 + set_turn_id 注入。
- MemoryMiddleware.aafter_model  : 清除 turn_id + 可选抽取（默认关）。
- MemoryMiddleware._extract_query : 从最后一条 HumanMessage 提 query。
- MemoryMiddleware._format_recall : 格式化召回结果 + token 预算裁剪。
- MemoryMiddleware._build_turn_id : 构造 turn_id（thread_id + 轮次）。

【职责边界】
- 只负责：召回、注入、token 裁剪、set_turn_id 生命周期、写 recalled_memories 索引。
- 不负责：记忆的存储与检索实现（memory 子系统）、记忆沉淀（MemoryConsolidation
  Middleware + worker）、强化写回（HybridRetriever 内部）、prompt 拼装（TaggedContext）。

【挂载位置】
Sandbox 之后，HelpRequest / ToolCall 之前。
放在这个位置的原因：记忆可以引用 sandbox 结果；注入形态是 user message，
不进入 tool pairing 结构。

【注入方式】
用 per-call HumanMessage 注入（保护 prompt caching，不进 system_prompt
的 cache prefix）。每条召回消息都带 hide_from_ui=True，UI 不展示。

【set_turn_id 生命周期】
before_model 注入，after_model 清除（traceability C），
用于把记忆操作关联到具体对话轮次。

【块 B 迁移】
从 agents/memory/middleware.py 迁到
agents/middlewares/memory_recall_middleware.py，与既有 18 个 middleware
同层治理。内容 1:1 搬迁，类名 MemoryMiddleware 保留。

【INVARIANT（必须保持的不变量）】
- 记忆是 middleware，不进 leader agent 主体（00 D9）。
- 不进 system prompt cache：用 per-call HumanMessage(hide_from_ui=True)（00 D10）。
- set_turn_id 注入 / 清除：before_model 注入，after_model 清除。
- recalled_memories 只存索引（id + score + strength），不存全量内容。
- 1A 强化写回在 HybridRetriever.retrieve 内部完成，caller 不负责。
- 无召回时清除 turn_id 后返回 None（避免 turn_id 悬挂）。
"""
from __future__ import annotations

import logging
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime

from poirot.backend.agents.memory.schema import MemoryTrace
from poirot.backend.agents.memory.strategies.default.manager import set_turn_id
from poirot.backend.agents.memory.types import MemoryQuery, RetrievalResult
from poirot.backend.agents.state.types import ThreadState

logger = logging.getLogger(__name__)

# token 估算：1 token ≈ 4 字符（简化估算，精确需 tiktoken）
_CHARS_PER_TOKEN = 4


class MemoryMiddleware(AgentMiddleware):
    """记忆召回中间件（before_model recall + after_model 清除）。

    挂载位置：Sandbox 后，HelpRequest / ToolCall 前。
    注入方式：per-call HumanMessage（保护 prompt caching）。

    Attributes:
        state_schema: 状态 schema（ThreadState）。
        _provider: 记忆提供者。
        _enable_recall: 召回开关。
        _enable_extract: 抽取开关（默认关）。
        _token_budget: 召回注入 token 预算。
    """

    state_schema = ThreadState  # type: ignore[assignment]

    def __init__(
        self,
        memory_provider: Any,
        *,
        enable_recall: bool = True,
        enable_extract: bool = False,
        token_budget: int = 2000,
    ) -> None:
        """初始化。

        Args:
            memory_provider: MemoryProvider（L3 DefaultMemoryProvider）。
            enable_recall: before_model 召回开关（default 模式可选关）。
            enable_extract: after_model 实时抽取开关（默认关，走 Phase 2 cron L5）。
            token_budget: 召回注入 token 预算上限。
        """
        self._provider = memory_provider
        self._enable_recall = enable_recall
        self._enable_extract = enable_extract
        self._token_budget = token_budget

    async def abefore_model(
        self, state: ThreadState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """before_model：召回 + 注入 per-call HumanMessage + set_turn_id 注入。

        处理流程：
        1. 若 enable_recall 为 False，直接返回 None（关闭召回）。
        2. _extract_query(state) 取 query；为空则返回 None。
        3. _build_turn_id(state, runtime) 构造 turn_id 并 set_turn_id 注入
           （traceability C，Layer 2 manager 的 operation_log.actor 会取它）。
        4. 调 provider.retriever().retrieve(MemoryQuery(text=query)) 召回：
           - L3 HybridRetriever 内部会做强化写回 1A + forgotten 过滤 3B；
           - 无结果则 set_turn_id(None) 清除后返回 None。
        5. _format_recall(results, token_budget) 做 token 预算裁剪 + 格式化。
        6. 返回 state patch：
           - messages += [HumanMessage(content=..., name="memory_recall",
                                       additional_kwargs={"hide_from_ui": True})]；
           - recalled_memories += [{"id", "score", "strength"}, ...]
             （只存索引，不存全量内容）。

        说明：1A 强化写回在 HybridRetriever.retrieve 内部完成，caller 不负责。

        Args:
            state: 当前 ThreadState，读取 messages。
            runtime: LangGraph 运行时，用于 _build_turn_id 取 thread_id。

        Returns:
            dict[str, Any] | None: 含 messages 与 recalled_memories 的 state patch；
                无召回时返回 None。
        """
        if not self._enable_recall:
            return None

        query = self._extract_query(state)
        if not query:
            return None

        # set_turn_id 注入（traceability C，Layer 2 manager operation_log.actor 取）
        turn_id = self._build_turn_id(state, runtime)
        set_turn_id(turn_id)

        # retrieve（L3 HybridRetriever，内部强化写回 1A + forgotten 过滤 3B）
        results = self._provider.retriever().retrieve(MemoryQuery(text=query))
        if not results:
            set_turn_id(None)  # 无召回，清除 turn_id
            return None

        # token budget 裁剪 + 格式化
        memories_text = self._format_recall(results, self._token_budget)

        # 注入 per-call HumanMessage（保护 prompt caching，不进 system prompt）
        return {
            "messages": [
                HumanMessage(
                    content=memories_text,
                    name="memory_recall",
                    additional_kwargs={"hide_from_ui": True},
                )
            ],
            # recalled_memories 只存索引（id + score + strength），不存全量内容
            "recalled_memories": [
                {"id": r.trace.id, "score": r.score, "strength": r.strength}
                for r in results
            ],
        }

    async def aafter_model(
        self, state: ThreadState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """after_model：清除 turn_id + 可选抽取（默认关，走 Phase 2 cron L5）。

        处理流程：
        1. 无条件 set_turn_id(None) 清除 turn_id
           （traceability C，无论 before_model 是否注入都清除）。
        2. 若 enable_extract 为 False，直接返回 None。
        3. 可选轻量抽取（默认关，走 Phase 2 cron L5）：
           Layer 4 仅保留 hook，不实现抽取逻辑，故仍返回 None。

        Args:
            state: 当前 ThreadState（本 hook 未使用）。
            runtime: LangGraph 运行时（本 hook 未使用）。

        Returns:
            dict[str, Any] | None: 始终 None（只做清除，不改 state）。
        """
        # 清除 turn_id（traceability C，无论 before_model 是否注入都清除）
        set_turn_id(None)

        if not self._enable_extract:
            return None

        # 可选轻量抽取（默认关，走 Phase 2 cron L5）
        # Layer 4 仅保留 hook，不实现抽取逻辑
        return None

    def _extract_query(self, state: ThreadState) -> str:
        """从最后一条 HumanMessage 中提取 query 文本。

        从后往前扫描 messages，找到最后一条 HumanMessage：
        - content 为 str：直接返回。
        - content 为 list（multimodal）：取第一段文本
          （若第一项是 dict 且含 "text"，取该字段）。
        - 其他：str(content) 兜底。

        找不到 HumanMessage 时返回空串。

        Args:
            state: 当前 ThreadState，读取 messages。

        Returns:
            str: 提取到的 query 文本；无则空串。
        """
        messages = state.get("messages", []) or []
        # 从后往前找最后一条 HumanMessage
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage):
                content = msg.content
                if isinstance(content, str):
                    return content
                # content 可能是 list（multimodal），取第一段文本
                if isinstance(content, list) and content:
                    first = content[0]
                    if isinstance(first, dict) and "text" in first:
                        return str(first["text"])
                return str(content)
        return ""

    def _format_recall(self, results: list[RetrievalResult], token_budget: int) -> str:
        """格式化召回结果 + 按 token 预算裁剪。

        处理流程：
        1. max_chars = token_budget * _CHARS_PER_TOKEN（1 token ≈ 4 字符）。
        2. 首行固定 "[Recalled Memories]"。
        3. 逐条结果拼成
           "[score=X.XX strength=Y.YY] <content>"，
           若加入该行会超过 max_chars 则 break（超预算截断）。
        4. 用 "\\n" 连接返回。

        Args:
            results: RetrievalResult 列表。
            token_budget: 注入 token 预算上限。

        Returns:
            str: 格式化后的召回文本。
        """
        max_chars = token_budget * _CHARS_PER_TOKEN
        lines: list[str] = ["[Recalled Memories]"]
        current_len = len(lines[0])
        for r in results:
            line = f"[score={r.score:.2f} strength={r.strength:.2f}] {r.trace.content}"
            if current_len + len(line) + 1 > max_chars:
                break  # 超预算截断
            lines.append(line)
            current_len += len(line) + 1
        return "\n".join(lines)

    def _build_turn_id(self, state: ThreadState, runtime: Runtime) -> str:
        """构造 turn_id（traceability C，关联记忆操作到对话轮次）。

        取 thread_id + 当前 message 数，拼成 "{thread_id}:turn:{N}"。
        thread_id 取值优先级：
        1. runtime.config["configurable"]["thread_id"]；
        2. state["thread_id"]；
        3. 字面量 "unknown"。
        取 runtime config 抛异常时退回 state["thread_id"]。

        Args:
            state: 当前 ThreadState，读取 messages / thread_id。
            runtime: LangGraph 运行时，读取 config.configurable.thread_id。

        Returns:
            str: 形如 "{thread_id}:turn:{len(messages)}" 的 turn 标识。
        """
        messages = state.get("messages", []) or []
        # thread_id 优先从 runtime config 取
        thread_id = "unknown"
        try:
            config = getattr(runtime, "config", None) or {}
            configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
            thread_id = configurable.get("thread_id") or state.get("thread_id") or "unknown"
        except Exception:
            thread_id = state.get("thread_id", "unknown")
        return f"{thread_id}:turn:{len(messages)}"