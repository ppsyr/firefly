"""MessageNormalizerMiddleware — wrap_model_call 合并多 SystemMessage 为单条 leading。

【为什么需要这个中间件】
严格后端（vLLM / Qwen / Anthropic 等）会拒绝「非 leading 位置的 SystemMessage」，
即要求整条请求里只能有一条 SystemMessage，且必须排在最前面。
这个中间件在 wrap_model_call 里把请求 payload 中出现的多条 SystemMessage
（含 request.system_message 与 messages 里的 SystemMessage）合并成一条，
放到 leading 位置，从而满足严格后端的要求。

【定位：公共固定件】
这是所有 agent 都需要的公共需求，因此作为固定件直接存在，不经过 registry
注册流程。如果把它的行为改成 noop（什么都不做），在严格后端上会直接因为
provider 报错而导致功能坏掉——所以它不是可选优化，而是必需件。

【整体职责】
在 wrap_model_call 阶段，把 request payload 中出现的多条 SystemMessage
（含 request.system_message 与 messages 里的 SystemMessage）合并成一条 leading
SystemMessage，满足严格后端（vLLM / Qwen / Anthropic）的格式要求。
只动 request payload，不动 checkpoint state。

【内容摘要】
- _flatten_content                : 把 SystemMessage.content 压平为字符串。
- MessageNormalizerMiddleware     : 合并多条 SystemMessage 为单条 leading。
- MessageNormalizerMiddleware._coalesce : 合并核心逻辑。
- wrap_model_call / awrap_model_call : 同步 / 异步入口。

【职责边界】
- 只负责：合并 request payload 里的多条 SystemMessage 为单条 leading。
- 不负责：生成 / 注入 SystemMessage（TaggedContextMiddleware 等）、
  压缩消息内容（StrategyMiddleware）、修改 checkpoint state。

【INVARIANT】
- 只动 request payload，不动 checkpoint state（history scanner 仍按原始结构工作）。
- 无 SystemMessage 可合并时返回 None（调用方用原 request）。
- 合并顺序：request.system_message 在最前，其后依次是 messages 里的 SystemMessage。
- content 合并：先 _flatten_content 压平，再用 "\\n\\n" 拼接。
- additional_kwargs 合并：后者覆盖前者。
- id 沿用第一条。
- 非 System 消息保持原顺序。
- 公共固定件：所有 agent 都需要，不经 registry 注册。

【为什么需要】
严格后端拒绝"非 leading 位置的 SystemMessage"——整条请求只能有一条 SystemMessage
且必须在最前。若把它改为 noop，严格后端会直接报错，功能坏掉——所以它不是可选优化，
而是必需件。

【与 TaggedContextMiddleware 的关系】
TaggedContextMiddleware 把上下文块包成一条 SystemMessage 放在最前；
本中间件负责把"已经存在的多条 SystemMessage"归拢成一条 leading。
两者都只作用于 request payload，不改 state。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import override

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import SystemMessage

from poirot.backend.agents.state.types import ThreadState


def _flatten_content(content: object) -> str:
    """把 SystemMessage 的 content 统一压平成字符串。

    SystemMessage.content 可能是多种形态，需要抹平后再参与合并：
    - str           → 原样返回。
    - list          → 逐项处理：
                        · str          → 直接取用；
                        · dict 含 text → 取 "text" 字段；
                        · 其他         → str(item) 兜底。
                      最后用 "\\n" 连接。
    - 其他类型      → str(content) 兜底。

    Args:
        content: SystemMessage 的 content 字段，形态不定。

    Returns:
        str: 压平后的字符串，用于后续以 "\\n\\n" 拼接合并。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and "text" in item:
                parts.append(item["text"])
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


class MessageNormalizerMiddleware(AgentMiddleware):
    """wrap_model_call 合并 SystemMessage 为单条 leading（公共固定）。

    严格后端（vLLM / Qwen / Anthropic）拒绝非 leading 的 SystemMessage。
    本中间件在模型调用前，把 request.system_message 与 request.messages
    中所有 SystemMessage 合并成一条，放在 leading 位置，其余非 System
    消息保持原顺序。

    只动 request payload，不动 checkpoint state；state_schema 绑定
    ThreadState 以保持与其他 middleware 一致。
    """

    state_schema = ThreadState  # type: ignore[assignment]

    @staticmethod
    def _coalesce(request: ModelRequest) -> ModelRequest | None:
        """把多条 SystemMessage 合并为单条 leading SystemMessage。

        处理流程：
        1. 收集 request.messages 中的所有 SystemMessage；
           若一条都没有，则返回 None（表示无需改动，调用方直接用原 request）。
        2. 组装待合并列表 parts：
           - 若 request.system_message 不为 None，先放入它（保证它在最前）；
           - 再依次放入 messages 里的 SystemMessage（保持原有相对顺序）。
        3. 以 parts[0] 为基准，合并所有 parts 的 additional_kwargs
           （后者覆盖前者，最终聚合为 merged_kwargs）。
        4. 合并 content：对每个 part 的 content 先 _flatten_content 压平，
           再用 "\\n\\n" 拼接。id 沿用第一条的 id。
        5. 从 request.messages 中剔除所有 SystemMessage，得到 non_system。
        6. 通过 request.override 返回新请求：
           - system_message = 合并后的单条；
           - messages       = non_system（原顺序保留）。

        Args:
            request: 原始模型调用请求。

        Returns:
            ModelRequest | None: 改写后的 ModelRequest；
                若没有任何 SystemMessage 可合并，返回 None。
        """
        in_msg_systems = [m for m in request.messages if isinstance(m, SystemMessage)]
        if not in_msg_systems:
            return None
        parts: list[SystemMessage] = []
        if request.system_message is not None:
            parts.append(request.system_message)
        parts.extend(in_msg_systems)
        first = parts[0]
        merged_kwargs: dict = {}
        for p in parts:
            merged_kwargs.update(p.additional_kwargs or {})
        merged = SystemMessage(
            content="\n\n".join(_flatten_content(p.content) for p in parts),
            id=first.id,
            additional_kwargs=merged_kwargs,
        )
        non_system = [m for m in request.messages if not isinstance(m, SystemMessage)]
        return request.override(system_message=merged, messages=non_system)

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        """同步模型调用入口：先尝试合并 SystemMessage，再交给下游 handler。

        _coalesce 返回 None 表示无需改动，此时直接把原 request 交给 handler。

        Args:
            request: 原始模型调用请求。
            handler: 下游处理函数。

        Returns:
            ModelCallResult: handler 返回的模型调用结果。
        """
        coalesced = self._coalesce(request)
        return handler(coalesced if coalesced is not None else request)

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        """异步模型调用入口：先尝试合并 SystemMessage，再 await 下游 handler。
        """
        coalesced = self._coalesce(request)
        return await handler(coalesced if coalesced is not None else request)