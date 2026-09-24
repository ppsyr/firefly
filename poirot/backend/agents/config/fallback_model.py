"""FallbackChatModel — 角色路由链 + 运行时故障降级。

【整体职责】
包装一组 BaseChatModel（按角色优先级链），对上层表现为单个 BaseChatModel。
调用时从当前活跃 provider 试起，遇瞬时 API 错误（限流 / 超时 / 连接 / 5xx）
自动降级到下一个；成功后记忆活跃 provider，避免每轮都从链首重试主力。
deepseek 作为链尾兜底。

【内容摘要】
- _should_fallback : 判断异常是否应触发降级（瞬时错误降级，客户端错误抛出）。
- FallbackChatModel: 降级链模型，实现 _generate / _agenerate / bind_tools。

【职责边界】
- 只负责：按链顺序调用、瞬时失败降级、记忆活跃 provider、对链内模型绑定 tools。
- 不负责：链的构造与选择（provider_config.route_chain_for / model_router）、
  模型实例化（build_chat_model）、角色路由表定义（MODEL_ROUTES）。

【INVARIANT】
- 链尾兜底：链由 route_chain_for 保证 deepseek 在链尾。
- 记忆活跃：_active 记录上次成功的索引，下轮从它试起（轮转起点），避免每轮重试主力。
- 仅瞬时错误降级：_should_fallback 为真才继续下一个，否则原样抛出。
- bind_tools 返回新实例：对链内每个 model 绑定，_active 重置（新对象默认 0）。
- 兼容两种形态：models 可为 BaseChatModel 或 bind_tools 后的 RunnableBinding，
  故统一用 invoke / ainvoke 调用。
"""
from __future__ import annotations

from typing import Any, override

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr


def _should_fallback(exc: Exception) -> bool:
    """是否应降级到下一个 provider。

    降级：网络 / 超时 / 限流 / 5xx 服务端错误（瞬时，换 provider 可能恢复）。
    不降级：400 / 401 / 404 等客户端错误（换 provider 也会失败，应暴露给上层）。

    Args:
        exc: 捕获到的异常。

    Returns:
        bool: True 表示应降级，False 表示应抛出。
    """
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    try:
        import openai

        if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError)):
            return True
        rate_limit = getattr(openai, "RateLimitError", None)
        if rate_limit is not None and isinstance(exc, rate_limit):
            return True
        status_err = getattr(openai, "APIStatusError", None)
        if status_err is not None and isinstance(exc, status_err):
            status = getattr(exc, "status_code", None)
            if status is not None and status >= 500:
                return True
    except ImportError:
        pass
    return False


class FallbackChatModel(BaseChatModel):
    """按链顺序调用，瞬时 API 失败降级到下一个，记忆活跃 provider。

    Attributes:
        models: 降级链，元素为 BaseChatModel 或其 bind_tools 后的 RunnableBinding。
        provider_names: 链内 provider 名，用于标识与 _identifying_params。
    """

    models: list[Any]  # BaseChatModel 或其 bind_tools 后的 RunnableBinding
    provider_names: list[str] = []
    _active: int = PrivateAttr(default=0)

    @override
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        """同步生成：从 _active 起轮转尝试链内模型，瞬时失败降级，成功则记忆。

        Args:
            messages: 输入消息。
            stop: 停止词。
            run_manager: 回调管理器。
            **kwargs: 透传给底层模型的参数。

        Returns:
            ChatResult: 包装为单条 ChatGeneration 的结果。

        Raises:
            Exception: 链内全部瞬时失败时抛出最后一个异常；
                或遇不可降级异常时立即抛出。
        """
        last_exc: Exception | None = None
        n = len(self.models)
        for offset in range(n):
            idx = (self._active + offset) % n
            try:
                # 用 invoke 兼容 BaseChatModel 与 bind_tools 后的 RunnableBinding
                ai: AIMessage = self.models[idx].invoke(messages, stop=stop, **kwargs)
                self._active = idx
                return ChatResult(generations=[ChatGeneration(message=ai)])
            except Exception as exc:
                if not _should_fallback(exc):
                    raise
                last_exc = exc
                continue
        assert last_exc is not None
        raise last_exc

    @override
    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        """异步生成：逻辑同 _generate，使用 ainvoke。

        Args:
            messages: 输入消息。
            stop: 停止词。
            run_manager: 异步回调管理器。
            **kwargs: 透传给底层模型的参数。

        Returns:
            ChatResult: 包装为单条 ChatGeneration 的结果。

        Raises:
            Exception: 链内全部瞬时失败时抛出最后一个异常；
                或遇不可降级异常时立即抛出。
        """
        last_exc: Exception | None = None
        n = len(self.models)
        for offset in range(n):
            idx = (self._active + offset) % n
            try:
                ai: AIMessage = await self.models[idx].ainvoke(messages, stop=stop, **kwargs)
                self._active = idx
                return ChatResult(generations=[ChatGeneration(message=ai)])
            except Exception as exc:
                if not _should_fallback(exc):
                    raise
                last_exc = exc
                continue
        assert last_exc is not None
        raise last_exc

    @override
    def bind_tools(self, tools: list[Any], **kwargs: Any) -> "FallbackChatModel":
        """对链内每个 model 绑定 tools，返回新的 FallbackChatModel（_active 重置）。

        Args:
            tools: 要绑定的工具列表。
            **kwargs: 透传给各 model.bind_tools 的参数。

        Returns:
            FallbackChatModel: 新的降级链模型，链内元素均为绑定后的 Runnable。
        """
        bound = [m.bind_tools(tools, **kwargs) for m in self.models]
        return FallbackChatModel(models=bound, provider_names=list(self.provider_names))

    @property
    @override
    def _llm_type(self) -> str:
        return "fallback-chat-model"

    @property
    @override
    def _identifying_params(self) -> dict[str, Any]:
        """标识参数：provider 列表与当前活跃索引。"""
        return {
            "providers": self.provider_names or [f"model-{i}" for i in range(len(self.models))],
            "active": self._active,
        }