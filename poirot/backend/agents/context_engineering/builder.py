"""治理 middleware 装配构建器。

【整体职责】
把「上下文治理」相关的 middleware 组装成一个有序列表，交给上层 Agent 挂载。
组装结果 = 公共 2 个固定 middleware + 1 个策略 middleware（可选）。

【组成】
1. 公共 2 固定（永远挂载，与所选策略无关）：
   - TaggedContextMiddleware        ：给消息上下文渲染标签（如 <date>）。
                                      DateInjector 的逻辑已并入此中间件，
                                      不再单独存在 DateInjector。
   - MessageNormalizerMiddleware    ：规范化消息结构（统一消息形态）。
2. StrategyMiddleware（按 config.strategy 选定的策略 bundle）：
   - 内部持有策略 bundle，负责路由 6 个 hook。
   - 若 config.strategy 对应的 bundle 未注册，则跳过该中间件并打 warning，
     仅挂载公共 2 个，不抛异常。
   - 过渡期兼容：像 "minimal" 这类尚未注册为 bundle 的策略名，只会挂公共 2 个，
     不会导致崩溃。

【挂载顺序（固定）】
公共 TaggedContextMiddleware
    → 公共 MessageNormalizerMiddleware
    → StrategyMiddleware（策略 bundle 的 6 个 hook）

【职责边界】
- token 预算 + 硬停：由策略 bundle 内部负责
  （DefaultStrategy 的 BudgetTrackerExecutor 计算 fraction + P5/P99 分段），
  公共层不再重复实现硬停逻辑。
- 公共层只负责：标签渲染、消息规范化、以及把策略 bundle 适配进 middleware 链。
"""

from __future__ import annotations

import logging
from typing import Any

from poirot.backend.agents.config.schema import ContextGovernanceConfig
from poirot.backend.agents.context_engineering import strategies  # noqa: F401  # 触发 bundle 注册
from poirot.backend.agents.context_engineering.registry import get_strategy_class
from poirot.backend.agents.context_engineering.strategy_middleware import StrategyMiddleware
from poirot.backend.agents.middlewares.message_normalizer_middleware import (
    MessageNormalizerMiddleware,
)
from poirot.backend.agents.middlewares.tagged_context_middleware import (
    TaggedContextMiddleware,
)

logger = logging.getLogger(__name__)


def build_governance_middlewares(
    config: ContextGovernanceConfig,
    model: Any = None,
    summarize_model: Any = None,
) -> list:
    """组装治理 middleware 列表。

    Args:
        config:         上下文治理配置对象，至少提供两个字段：
                        - strategy: 策略 bundle 名称（用于注册表查表）。
                        - params  : 传给策略 bundle 构造函数的参数。
        model:          主模型对象，透传给策略 bundle（供策略内部按需使用）。
        summarize_model: 摘要专用模型对象，透传给策略 bundle
                         （供策略内部做上下文摘要时使用）。

    Returns:
        有序的 middleware 列表，顺序为
        [TaggedContextMiddleware, MessageNormalizerMiddleware, (StrategyMiddleware)]。
        其中 StrategyMiddleware 仅在策略 bundle 注册成功时存在。

    Raises:
        不向外抛异常。策略未注册的情况被捕获并降级为「仅公共 2 个」。

    组装规则：
        1. 先固定放入公共 2 个 middleware：
           - TaggedContextMiddleware
           - MessageNormalizerMiddleware
        2. 根据 config.strategy 从注册表查找策略 bundle 类：
           - 查得到：用 config.params 实例化 bundle（同时传入 model / summarize_model），
             再用该 bundle 构造 StrategyMiddleware，追加到列表末尾。
           - 查不到（KeyError）：记录 warning，直接返回公共 2 个 middleware，
             不追加 StrategyMiddleware，不抛异常。
    """
    # 公共 2 个固定 middleware，顺序不可调换（先标签渲染，再消息规范化）
    middlewares: list = [
        TaggedContextMiddleware(),
        MessageNormalizerMiddleware(),
    ]

    # 按 config.strategy 查策略 bundle 类；未注册则降级跳过策略中间件
    try:
        bundle_cls = get_strategy_class(config.strategy)
    except KeyError:
        # 过渡期兼容：像 minimal 这类非 bundle 名只挂公共 2 个，不崩
        logger.warning(
            "strategy bundle '%s' not registered, skip StrategyMiddleware (public 2 only)",
            config.strategy,
        )
        return middlewares

    # 实例化策略 bundle，并包成 StrategyMiddleware 追加到链尾
    bundle = bundle_cls(config.params, model=model, summarize_model=summarize_model)
    middlewares.append(StrategyMiddleware(bundle, config.params))
    return middlewares