"""Run journal package — 运行日志模块。

【整体职责】
记录一次运行过程中的事件流：定义运行事件的数据契约与时间工具，并把事件以追加方式
写入 events.jsonl。与 RunRecord（状态快照）互补——一个记"经历过什么"，一个记"现在是什么"。

【内容摘要】
- events      : 事件契约与时间工具（RunEvent / utc_now_iso）。
- run_journal : 事件写入器（RunJournal / _make_event_id）。

【职责边界】
- 只负责：运行事件的数据结构、时间戳工具、事件追加写入。
- 不负责：事件的产生（run_manager / activity_tracker）、事件的读取与消费
  （TUI / CLI / 审计）、运行状态的推进（run_manager）。

【两层结构】
- 事件定义：events（RunEvent + utc_now_iso）。
- 事件写入：run_journal（RunJournal.append → events.jsonl）。

【事件族】
- run.*      ：run.started / run.finished / run.failed（由 RunManager 发出）。
- activity.* ：activity.started / activity.finished / activity.heartbeat（由 ActivityTracker 发出）。
"""
from poirot.backend.agents.journal.events import RunEvent, utc_now_iso
from poirot.backend.agents.journal.run_journal import RunJournal

__all__ = ["RunEvent", "utc_now_iso", "RunJournal"]