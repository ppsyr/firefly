"""日志事件定义 — 运行事件的数据契约与时间工具。

【整体职责】
定义运行事件（RunEvent）的数据结构：事件标识、所属 run、事件类型、payload 与
创建时间，并提供统一的时间戳工具 utc_now_iso()。供 RunJournal 写入事件流、
RunManager 记录时间戳。

【内容摘要】
- _CST           : 东八区时区常量。
- RunEvent       : 运行事件，frozen dataclass，含 to_dict 序列化。
- utc_now_iso    : 生成当前时间的 ISO 字符串。

【职责边界】
- 只负责：定义运行事件的数据结构、字段语义、序列化与时间戳工具。
- 不负责：事件的写入与持久化（run_journal）、事件的产生与消费
  （run_manager / activity_tracker / middleware）。

【INVARIANT】
- frozen：构造后不可变，可跨组件 / 跨线程安全共享。
- 必填字段：event_id / run_id / event_type / payload / created_at 均无默认值。
- payload 为 dict：承载事件的具体内容，形态由 event_type 约定。
- to_dict 直接 asdict：字段与序列化结果一一对应（无枚举需转换）。
- 时间戳为 ISO 字符串：created_at 由 utc_now_iso() 生成。

【命名说明】
函数名为 utc_now_iso，但实现使用东八区（_CST）而非 UTC。命名与实现存在偏差，
此处仅为标注，不修改代码。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
from typing import Any

_CST = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class RunEvent:
    """运行事件。

    Attributes:
        event_id: 事件唯一标识。
        run_id: 所属运行 ID。
        event_type: 事件类型（如 run.started / activity.finished）。
        payload: 事件内容，形态由 event_type 约定。
        created_at: 创建时间（ISO 字符串）。
    """

    event_id: str
    run_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict。

        Returns:
            dict[str, Any]: 字段与值一一对应的字典。
        """
        return asdict(self)


def utc_now_iso() -> str:
    """生成当前时间的 ISO 字符串（使用东八区 _CST）。

    Returns:
        str: 形如 "2024-01-01T12:00:00+08:00" 的时间字符串。
    """
    return datetime.now(_CST).isoformat()