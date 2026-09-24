"""策略 bundle 实现包。

【整体职责】
strategies/ 下每个子包是一个独立的策略 bundle，实现 GovernanceStrategy 的 6 对 hook。
本 __init__.py 负责「导入即触发注册」：
    - import 本包 → import 各策略子包 → 子包内 @register_strategy 装饰器执行
    - 注册表 registry._STRATEGY_BUNDLES 被填充
    - 上层 builder.build_governance_middlewares 才能按 config.strategy 查到 bundle 类

【导入即注册的机制】
    strategies/__init__.py
        └─ from ...strategies.default import DefaultStrategy
              └─ strategies/default/__init__.py
                    └─ from ...strategies.default.strategy import DefaultStrategy
                          └─ strategy.py 中 @register_strategy("default")
                                → _STRATEGY_BUNDLES["default"] = DefaultStrategy

【新增策略 bundle 的做法】
    1. 在 strategies/ 下新建子包，如 strategies/my_strategy/；
    2. 子包内实现 bundle 类，类上加 @register_strategy("my_strategy")；
    3. 子包 __init__.py 导出该类；
    4. 在本文件 import 该子包（或该类），触发注册。

【当前已注册策略】
    - "default" → DefaultStrategy（strategies/default/strategy.py）

【与 registry / builder 的关系】
    - registry.register_strategy(name)：装饰器，写 _STRATEGY_BUNDLES。
    - registry.get_strategy_class(name)：查表，未注册抛 KeyError。
    - builder.build_governance_middlewares：查表后实例化 bundle，
      包成 StrategyMiddleware 挂进 Agent；未注册时降级为「仅公共 2 个」。
"""

from __future__ import annotations

# 导入即触发 strategies/default/strategy.py 的 @register_strategy("default") 注册
from poirot.backend.agents.context_engineering.strategies.default import DefaultStrategy

__all__ = ["DefaultStrategy"]