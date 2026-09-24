"""token utility：token_counter + resolve_window_size + resolve_model_name。

【整体职责】
为上下文治理层提供「token 计数」与「模型窗口/模型名解析」两类工具：
    1. token_counter(messages, model_name=None) → int
       算 messages 的总 token 数。tiktoken 优先；tiktoken 不可用时退化到
       CJK-aware 字符估算（CJK 字符 ~1 token，非 CJK ~4 字符/token）。
    2. resolve_window_size(model) → int
       取模型上下文窗口大小。支持穿透 FallbackChatModel 拿到内层真实模型，
       再按属性 / _identifying_params / model_name 前缀映射逐级解析。
    3. resolve_model_name(model) → str | None
       取当前活跃 provider 的真实模型名（如 "deepseek-v4-flash"），
       与 resolve_window_size 共用穿透逻辑。

【被谁用】
    - StrategyMiddleware._ctx 把 token_counter 注入 GovernanceContext；
    - DefaultStrategy.after_model 调 resolve_window_size 解析 window；
    - DefaultStrategy.budget.track 用 ctx.token_counter 算 current/fraction；
    - TUI 的 Build 信息行用 resolve_model_name 展示真实模型名。

【关键设计】
    - tiktoken 懒加载 + 失败 cooldown（600s）+ 并发 LOADING sentinel 缓存；
    - CJK-aware char fallback：中文场景下比"字符数/4"更接近真实 token；
    - FallbackChatModel 穿透：本身只含 providers/active，必须进 .models[active]
      才能拿到真实容量与模型名；bind_tools 后的 RunnableBinding 需剥 .bound。
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# tiktoken 失败后进入冷却期，期间不再重试，直接走 char 估算
_TIKTOKEN_RETRY_COOLDOWN_S = 600
# 无法解析时的默认窗口大小
_DEFAULT_WINDOW = 128_000

# model_name → window 映射表。长前缀优先（避免 "gpt-4" 误匹配 "gpt-4o"）。
# langchain ChatModel 不一定暴露 max_input_tokens，靠 model_name 前缀匹配兜底。
_MODEL_WINDOW_MAP: dict[str, int] = {
    # OpenAI（长前缀优先，避免 gpt-4 误匹配 gpt-4o）
    "gpt-4o-mini": 128_000,
    "gpt-4o": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4-110": 128_000,
    "gpt-4": 8_192,
    "gpt-3.5-turbo": 16_385,
    "o3-mini": 200_000,
    "o3": 200_000,
    "o1-mini": 128_000,
    "o1": 200_000,
    # Anthropic
    "claude-3-5-sonnet": 200_000,
    "claude-3-5-haiku": 200_000,
    "claude-3-opus": 200_000,
    "claude-3-sonnet": 200_000,
    "claude-3-haiku": 200_000,
    # DeepSeek
    "deepseek-v4-flash": 200_000,
    "deepseek-reasoner": 64_000,
    "deepseek-chat": 64_000,
    # Qwen
    "qwen-max": 32_768,
    "qwen-plus": 131_072,
    "qwen-turbo": 131_072,
    "qwen2.5": 131_072,
    "qwen": 32_768,
    # GLM
    "glm-4-plus": 131_072,
    "glm-4": 131_072,
    # Yi
    "yi-large": 32_768,
    "yi-34b": 4_096,
    # Moonshot
    "moonshot-v1-128k": 131_072,
    "moonshot-v1-32k": 32_768,
    "moonshot-v1-8k": 8_192,
    "moonshot-v1": 8_192,
    # Stepfun
    "step-2": 8_192,
    "step-1": 8_192,
    # MiniMax
    "abab6.5": 245_760,
    # xAI Grok
    "grok-3": 131_072,
    "grok-2": 131_072,
    # Google Gemini
    "gemini-2.0-flash": 1_000_000,
    "gemini-2.5-flash": 1_000_000,
    "gemini-2.5-pro": 2_000_000,
    "gemini-1.5-flash": 1_000_000,
    "gemini-1.5-pro": 2_000_000,
    "gemini": 1_000_000,
    # Anthropic Claude 4+（3.x 已在上方）
    "claude-sonnet-4": 200_000,
    "claude-opus-4": 200_000,
    "claude-haiku-4": 200_000,
    "claude": 200_000,
    # vLLM / openai-compatible 默认
    "vllm": 128_000,
}

# encoding 缓存：key 为 model_name 或 "default"，value 为 encoding 或 _loading_sentinel
_ENCODING_CACHE: dict[str, Any] = {}
# 上次 tiktoken 失败时间戳，用于 cooldown 判定
_last_failure_ts: float = 0.0
# 并发占位 sentinel：表示"该 key 正在加载/加载失败"，避免重复尝试
_loading_sentinel = object()

# CJK Unified Ideographs + Extension A + CJK Symbols
_CJK_RANGES = ((0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0x3000, 0x303F))


def _is_cjk(ch: str) -> bool:
    """判断单字符是否属于 CJK 区间。

    Args:
        ch: 单个字符。

    Returns:
        True 表示落在 _CJK_RANGES 任一区间内。

    用途：
        char 估算时 CJK 字符按 ~1 token 计，非 CJK 按 ~4 字符/token。
    """
    o = ord(ch)
    return any(lo <= o <= hi for lo, hi in _CJK_RANGES)


def _extract_text(message: Any) -> str:
    """从 message（str / BaseMessage / dict）提取纯文本。

    Args:
        message: 可能是 str、BaseMessage（有 .content）或 dict（含 "content"）。

    Returns:
        提取出的纯文本；content 为 list 时把各 part 的 text 拼接。

    支持格式：
        - str                          → 直接返回
        - BaseMessage                  → 读 .content
        - dict                         → 读 ["content"]
        - content 为 str               → 直接返回
        - content 为 list[str|dict]    → 拼接 str / dict["text"]
        - 其它                          → str(content) 兜底
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


def _char_estimate(messages: list) -> int:
    """CJK-aware char 估算：CJK 字符 ~1 token，非 CJK ~4 字符/token。

    Args:
        messages: 消息列表。

    Returns:
        估算 token 数 = CJK 字符数 + 非 CJK 字符数 // 4。

    用途：
        tiktoken 不可用时的 fallback；比"字符数/4"更贴近中文真实 token。
    """
    cjk = 0
    other = 0
    for msg in messages:
        for ch in _extract_text(msg):
            if _is_cjk(ch):
                cjk += 1
            else:
                other += 1
    return cjk + other // 4


def _get_encoding(model_name: str | None) -> Any | None:
    """tiktoken encoding 懒加载 + 失败 cooldown + 并发 LOADING sentinel。

    Args:
        model_name: 模型名；None 时用 "default" 键 + cl100k_base。

    Returns:
        tiktoken encoding 对象；不可用时返回 None（调用方走 char 估算）。

    缓存策略：
        - key 命中且非 sentinel → 直接返回缓存的 encoding；
        - key 命中且是 sentinel → 返回 None（加载中或曾失败）；
        - 距上次失败 < cooldown → 直接返回 None，不重试；
        - 否则尝试 import tiktoken 并取 encoding：
            成功 → 缓存 encoding 并返回；
            失败 → 记录失败时间戳 + 缓存 sentinel + 返回 None。
    """
    global _last_failure_ts
    key = model_name or "default"
    if key in _ENCODING_CACHE:
        cached = _ENCODING_CACHE[key]
        return None if cached is _loading_sentinel else cached
    if time.time() - _last_failure_ts < _TIKTOKEN_RETRY_COOLDOWN_S:
        return None
    try:
        import tiktoken  # type: ignore[import-untyped]

        enc = tiktoken.encoding_for_model(key) if model_name else tiktoken.get_encoding("cl100k_base")
    except Exception as exc:
        logger.warning("tiktoken unavailable, fallback to char estimate: %s", exc)
        _last_failure_ts = time.time()
        _ENCODING_CACHE[key] = _loading_sentinel
        return None
    _ENCODING_CACHE[key] = enc
    return enc


def token_counter(messages: list, model_name: str | None = None) -> int:
    """算 messages 总 token。tiktoken 优先，失败 fallback char 估算。

    Args:
        messages:   消息列表（元素可为 str / BaseMessage / dict）。
        model_name: 模型名；用于选 tiktoken encoding。None 走 cl100k_base。

    Returns:
        messages 总 token 数。

    流程：
        1. _get_encoding(model_name) 取 encoding；
        2. encoding 为 None → 直接 _char_estimate(messages)；
        3. 否则逐条 encode(_extract_text(msg)) 累加长度；
           单条 encode 异常 → 该条退化为 _char_estimate([msg])。

    被谁用：
        StrategyMiddleware._ctx 把它注入 GovernanceContext.token_counter，
        DefaultStrategy.budget.track 用它算 current/fraction。
    """
    enc = _get_encoding(model_name)
    if enc is None:
        return _char_estimate(messages)
    total = 0
    for msg in messages:
        try:
            total += len(enc.encode(_extract_text(msg)))
        except Exception:
            total += _char_estimate([msg])
    return total


def resolve_window_size(model: Any) -> int:
    """取模型上下文窗口大小。

    解析顺序：
    1. FallbackChatModel 穿透——读 ``.models[active]`` 内层真实模型（ChatDeepSeek
       等），递归解析其 model_name → 命中映射表（deepseek-v4-flash→200k 等）。
       FallbackChatModel 本身的 _identifying_params 只含 providers/active，不含
       窗口信息，必须穿透到内层才能拿到真实容量。
    2. model 属性（max_input_tokens / model_max_tokens / max_tokens）
    3. _identifying_params dict
    4. model_name / model 前缀匹配 _MODEL_WINDOW_MAP
    5. default 128000

    Args:
        model: langchain ChatModel 或 FallbackChatModel 包装。

    Returns:
        解析到的窗口大小；全部失败时返回 _DEFAULT_WINDOW。

    被谁用：
        DefaultStrategy.after_model 解析 window（config.window 优先，否则走本函数）。
    """
    # FallbackChatModel 穿透：取活跃内层模型递归解析
    inner_models = getattr(model, "models", None)
    if isinstance(inner_models, list) and inner_models:
        active = getattr(model, "_active", 0) or 0
        inner = inner_models[active] if active < len(inner_models) else inner_models[0]
        # bind_tools 后的 RunnableBinding 需剥 .bound 拿到原始 BaseChatModel
        bound = getattr(inner, "bound", None)
        if bound is not None:
            inner = bound
        w = resolve_window_size(inner)
        if w != _DEFAULT_WINDOW:
            return w
    # 模型属性直读
    for attr in ("max_input_tokens", "model_max_tokens", "max_tokens"):
        value = getattr(model, attr, None)
        if isinstance(value, int) and value > 0:
            return value
    # _identifying_params（可能是 callable）
    params = getattr(model, "_identifying_params", None)
    if callable(params):
        try:
            params = params()
        except Exception:
            params = None
    if isinstance(params, dict):
        for k in ("max_input_tokens", "model_max_tokens", "max_tokens"):
            v = params.get(k)
            if isinstance(v, int) and v > 0:
                return v
    # model_name / model 前缀匹配映射表
    model_name = getattr(model, "model_name", None) or getattr(model, "model", None)
    if isinstance(model_name, str):
        for prefix, window in _MODEL_WINDOW_MAP.items():
            if model_name.startswith(prefix):
                return window
    return _DEFAULT_WINDOW


def resolve_model_name(model: Any) -> str | None:
    """取当前活跃 provider 的真实模型名（如 ``deepseek-v4-flash``）。

    与 ``resolve_window_size`` 同一穿透逻辑：FallbackChatModel 本身没有模型名，
    只有 ``.models[active]`` 内层真实 ChatModel 才有 ``model_name``/``model``
    属性——TUI 的 Build 信息行要展示的是这个真实名字，不是 provider 链
    （如 ``openai,qwen,deepseek``）。

    Args:
        model: langchain ChatModel 或 FallbackChatModel 包装。

    Returns:
        真实模型名；全部失败时返回 None。

    解析顺序：
        1. FallbackChatModel 穿透到 .models[active]（剥 .bound），递归；
        2. model.model_name / model.model；
        3. _identifying_params 的 model_name / model；
        4. 都没有 → None。

    被谁用：
        TUI Build 信息行展示真实模型名（而非 provider 链）。
    """
    # FallbackChatModel 穿透
    inner_models = getattr(model, "models", None)
    if isinstance(inner_models, list) and inner_models:
        active = getattr(model, "_active", 0) or 0
        inner = inner_models[active] if active < len(inner_models) else inner_models[0]
        bound = getattr(inner, "bound", None)
        if bound is not None:
            inner = bound
        name = resolve_model_name(inner)
        if name:
            return name
    # 直读属性
    name = getattr(model, "model_name", None) or getattr(model, "model", None)
    if isinstance(name, str) and name:
        return name
    # _identifying_params
    params = getattr(model, "_identifying_params", None)
    if callable(params):
        try:
            params = params()
        except Exception:
            params = None
    if isinstance(params, dict):
        v = params.get("model_name") or params.get("model")
        if isinstance(v, str) and v:
            return v
    return None