"""默认策略的公共常量。

目前只有 CST（中国标准时间，UTC+8）。
strategy / externalizer / snapshot / summarizer 里凡是要写本地时间戳的地方都用它，
保证所有 compaction 产物（jsonl / snapshot / summary_id）时间口径一致。
"""

from datetime import timedelta, timezone

# 中国标准时间 UTC+8，写日志/快照/摘要 id 统一用它
CST = timezone(timedelta(hours=8))