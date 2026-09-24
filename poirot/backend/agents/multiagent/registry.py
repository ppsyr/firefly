"""SpecialistRegistry — specialist 注册发现（非全局单例）。

【整体职责】
维护"名字 → specialist 实例"的映射，供 Leader / bootstrap 查询与过滤 specialist。
作为 CapabilityRegistry 的一个能力注入，不做全局单例；装配期由 bootstrap 反射填充。

【内容摘要】
- SpecialistRegistry      : 注册表主类（内部可变 dict）。
- register()              : 注册 specialist，返回其名字。
- get()                   : 按名字取 specialist，缺失抛 SpecialistNotFoundError。
- list_specialists()      : 列出 specialist 名字，可按能力过滤。
- register_from_config()  : 装配期反射加载入口（当前为接口契约，未实现）。
- __len__ / __contains__  : 支持 len() 与 in 查询。

【职责边界】
- 只负责：注册、查询、按能力过滤、长度/成员判断。
- 不负责：specialist 的实例化（bootstrap 负责）、凭证检测（bootstrap 负责）、
  实际派活（Leader 负责）。
- 非全局单例：每个 CapabilityRegistry 持有自己的 registry 实例。

【INVARIANT】
- 非 frozen：内部 dict 可变，注册动作在装配期发生。
- 同名覆盖：register 采用 last-write-wins，不报错。
- get 缺失抛 SpecialistNotFoundError，不返回 None。
- duck typing：接受任何实现 SpecialistAgent Protocol 的对象，不要求显式继承。
- capability=None 时 list_specialists 返回全部名字。
- register_from_config 抛 NotImplementedError：装配逻辑在 bootstrap 层实现。
"""
from __future__ import annotations

from poirot.backend.agents.multiagent.exceptions import SpecialistNotFoundError
from poirot.backend.agents.multiagent.specialist import SpecialistAgent
from poirot.backend.agents.multiagent.types import SpecialistCapability


class SpecialistRegistry:
    """specialist 注册发现（内部可变 dict，非 frozen）。

    作为 CapabilityRegistry 的能力注入使用；装配期由 bootstrap 反射加载。
    """

    def __init__(self) -> None:
        self._specialists: dict[str, SpecialistAgent] = {}

    def register(self, specialist: SpecialistAgent) -> str:
        """注册 specialist，返回 specialist.name。同名覆盖（last-write-wins）。"""
        name = specialist.name
        self._specialists[name] = specialist
        return name

    def get(self, name: str) -> SpecialistAgent:
        """按名字获取 specialist。缺失抛 SpecialistNotFoundError。"""
        try:
            return self._specialists[name]
        except KeyError:
            raise SpecialistNotFoundError(name)

    def list_specialists(
        self,
        capability: SpecialistCapability | None = None,
    ) -> list[str]:
        """列出 specialist 名字。capability=None 返回全部；否则按能力过滤。"""
        names = list(self._specialists.keys())
        if capability is None:
            return names
        return [
            name
            for name in names
            if self._specialists[name].capabilities.has(capability)
        ]

    def register_from_config(self, use_list: list[str]) -> None:
        """反射加载 specialist（装配期由 bootstrap 调用）。

        预期行为：按 use_list 反射 import specialist 类 + 凭证检测 +
        disabled 不注册。当前留接口契约，由 bootstrap 实现。
        """
        raise NotImplementedError(
            "register_from_config implemented in bootstrap (Batch 10)"
        )

    def __len__(self) -> int:
        """返回已注册 specialist 数量。"""
        return len(self._specialists)

    def __contains__(self, name: object) -> bool:
        """判断某名字是否已注册。"""
        return name in self._specialists