"""策略 bundle 注册表。

【整体职责】
维护一张全局映射表 ``name -> bundle_cls``，用于「按名字查找策略 bundle 类」。
策略 bundle 通过 ``@register_strategy(name)`` 装饰器被动注册；
装配器（builder）通过 ``get_strategy_class(name)`` 查表拿到类再实例化。

【使用方式】
新增一个策略 bundle，只需：
    1. 在新文件里定义 bundle 类；
    2. 在类上加 ``@register_strategy("策略名")``；
    3. 在某个会被 import 的地方 import 该文件，触发注册（import 副作用）。

【设计要点】
- 注册表是模块级全局变量，进程内共享。
- 重注册（同名不同类）不报错，仅打 warning，后注册的覆盖先注册的，
  方便排查策略被意外覆盖。
- ``clear_strategies()`` 用于测试隔离，避免用例间互相污染。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

# 全局注册表：策略名 -> 策略 bundle 类
_STRATEGY_BUNDLES: dict[str, type] = {}


def register_strategy(name: str) -> Callable[[type], type]:
    """装饰器工厂：把策略 bundle 类注册到全局表。

    Args:
        name: 策略 bundle 的注册名（供 config.strategy 查表使用）。

    Returns:
        一个类装饰器。被装饰的类会以 ``name`` 为键写入 ``_STRATEGY_BUNDLES``，
        装饰器原样返回该类（不改变类本身）。

    行为细节：
        - 若同名已注册且不是同一个类，打 warning 提示重注册
          （旧类名 -> 新类名），随后仍以新类覆盖旧类。
        - 若同名已注册且就是同一个类，视为幂等，不告警。
    """

    def decorator(cls: type) -> type:
        if name in _STRATEGY_BUNDLES and _STRATEGY_BUNDLES[name] is not cls:
            logging.getLogger(__name__).warning(
                "strategy bundle '%s' re-registered: %s -> %s",
                name,
                _STRATEGY_BUNDLES[name].__name__,
                cls.__name__,
            )
        _STRATEGY_BUNDLES[name] = cls
        return cls

    return decorator


def get_strategy_class(name: str) -> type:
    """按名查策略 bundle 类。

    Args:
        name: 策略 bundle 的注册名。

    Returns:
        对应的策略 bundle 类。

    Raises:
        KeyError: 该名字未注册。异常信息里附带当前所有已注册名，
                  便于排查拼写错误或忘记 import 触发注册。
    """
    if name not in _STRATEGY_BUNDLES:
        raise KeyError(
            f"strategy bundle '{name}' not registered. "
            f"Available: {sorted(_STRATEGY_BUNDLES)}"
        )
    return _STRATEGY_BUNDLES[name]


def list_strategies() -> list[str]:
    """列出所有已注册策略 bundle 名（按字典序排序）。

    Returns:
        已注册策略名的排序列表，主要用于调试与展示。

    注意：
        返回的是排序后的新列表，不影响内部注册表顺序。
    """
    return sorted(_STRATEGY_BUNDLES)


def clear_strategies() -> None:
    """清空策略 bundle 注册表。

    主要用于测试隔离：避免上一个用例注册的策略污染下一个用例。
    生产代码通常不应调用。
    """
    _STRATEGY_BUNDLES.clear()