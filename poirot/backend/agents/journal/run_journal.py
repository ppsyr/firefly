"""RunJournal — 事件流的写入器。

【整体职责】
把运行期事件以追加方式写入 events.jsonl：为每个事件生成 event_id、补充 run_id 与
时间戳，序列化为 JSON 后落盘。供 RunManager 记录 run 生命周期事件、
RunActivityTracker 等记录活动事件。

【内容摘要】
- _make_event_id : 生成 event_id（UTC 毫秒精度时间戳 + 随机后缀）。
- RunJournal     : 运行日志写入器，含 append 方法。

【职责边界】
- 只负责：构造 RunEvent、追加写入 events.jsonl。
- 不负责：事件的数据结构定义（events）、事件的产生与消费
  （run_manager / activity_tracker / middleware）、事件流的读取与查询。

【INVARIANT】
- 追加写入：以 "a" 模式打开，事件只增不改。
- 每事件一行块：JSON 序列化（ensure_ascii=False, indent=2）+ 两个换行分隔。
- 目录自动创建：写入前 mkdir(parents=True, exist_ok=True)。
- event_id 唯一：UTC 毫秒精度时间戳 + 4 位随机后缀。
- run_id 由构造时绑定：append 不接受 run_id，统一用实例的 run_id。
- payload 可选：None 时置为空 dict。
- 时间戳用 utc_now_iso()：与 RunEvent.created_at 一致。
"""
from __future__ import annotations

import json
import random
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from poirot.backend.agents.journal.events import RunEvent, utc_now_iso


def _make_event_id() -> str:
    """生成 event_id：UTC 毫秒精度时间戳 + 4 位随机后缀。

    Returns:
        str: 形如 "evt-20240101T120000123-ab3d" 的唯一标识。
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")[:-3]  # ms precision
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"evt-{ts}-{suffix}"


class RunJournal:
    """运行日志写入器：把事件追加到 events.jsonl。

    Attributes:
        run_id: 所属运行 ID。
        events_path: 事件文件路径（events.jsonl）。
    """

    def __init__(self, run_id: str, events_path: str | Path) -> None:
        """初始化。

        Args:
            run_id: 所属运行 ID。
            events_path: 事件文件路径。
        """
        self.run_id = run_id
        self.events_path = Path(events_path)

    def append(self, event_type: str, payload: dict[str, Any] | None = None) -> RunEvent:
        """追加一条事件到 events.jsonl。

        Args:
            event_type: 事件类型（如 run.started / run.finished）。
            payload: 事件内容，可选；None 时置为空 dict。

        Returns:
            RunEvent: 写入的事件对象。

        流程：
            1. 确保父目录存在。
            2. 构造 RunEvent（生成 event_id，补 run_id 与 created_at）。
            3. 追加写入 JSON（ensure_ascii=False, indent=2 + 双换行）。
        """
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        event = RunEvent(
            event_id=_make_event_id(),
            run_id=self.run_id,
            event_type=event_type,
            payload=payload or {},
            created_at=utc_now_iso(),
        )
        with self.events_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(event.to_dict(), ensure_ascii=False, indent=2) + "\n\n")
        return event