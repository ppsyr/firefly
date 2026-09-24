"""SnapshotExecutor — P4 压缩前快照执行器。

【整体职责】
P4（fraction >= 0.80）压缩前，把当前 messages + 关键 state 字段落盘成 JSON 快照，
防止摘要不可逆地丢信息；同时在 governance 里记录 snapshot_path 和 snapshot_count。

【与 strategy.py 的关系】
    strategy.before_model 的 P4 分支先调 snapshot_if_pending；
    拿到返回的 governance 后，再把它传给 summarizer.summarize_if_pending。
    snapshot 失败（写盘失败）返回 None，strategy 会 fallback 用原 governance 继续摘要。

【快照内容】
    created_at / messages（序列化后）/
    research_question / todos / observations / reflection_items
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any

from poirot.backend.agents.context_engineering.strategies.default._constants import CST

logger = logging.getLogger(__name__)


class SnapshotExecutor:
    """P4 压缩前存快照 + 记 path。"""

    def __init__(self, snapshot_dir: str = ".poirot/snapshots") -> None:
        """构造快照器。

        Args:
            snapshot_dir: 快照目录。
        """
        self._dir = snapshot_dir

    def snapshot_if_pending(self, governance: dict | None, messages: list, state: Any) -> dict | None:
        """若 pending 含 P4，则构建并写盘快照，把 path/count 写回 governance。

        Args:
            governance: 现有 governance。
            messages:   当前消息列表。
            state:      当前 ThreadState。

        Returns:
            更新后的 governance（default.snapshot_path / default.metrics.snapshot_count）；
            非 P4 pending 或写盘失败返回 None。
        """
        governance = governance or {}
        pending = (governance.get("default") or {}).get("pending") or []
        if "P4" not in pending:
            return None
        logger.info("snapshot: P4 pending, building snapshot (msgs=%d, dir=%s)", len(messages), self._dir)
        snapshot = self._build_snapshot(messages, state)
        path = self._write_to_disk(snapshot)
        if path is None:
            logger.error("snapshot: _write_to_disk failed, snapshot NOT saved")
            return None
        logger.info("snapshot: saved to %s", path)
        g = dict(governance)
        d = dict(g.get("default") or {})
        d["snapshot_path"] = path
        metrics = dict(d.get("metrics") or {})
        metrics["snapshot_count"] = metrics.get("snapshot_count", 0) + 1
        d["metrics"] = metrics
        g["default"] = d
        return g

    def _build_snapshot(self, messages: list, state: Any) -> dict:
        """构造快照 dict。

        Args:
            messages: 消息列表（逐条 _serialize_msg）。
            state:    ThreadState（取 research_question / todos / observations / reflection_items）。

        Returns:
            可 json.dump 的快照 dict。
        """
        state = state or {}
        return {
            "created_at": datetime.now(CST).isoformat(),
            "messages": [self._serialize_msg(m) for m in messages],
            "research_question": state.get("research_question"),
            "todos": self._serialize_list(state.get("todos")),
            "observations": self._serialize_list(state.get("observations")),
            "reflection_items": self._serialize_list(state.get("reflection_items")),
        }

    @staticmethod
    def _serialize_list(items: Any) -> list:
        """把 dataclass 列表转成 dict 列表，防 json.dump 序列化失败。

        Args:
            items: 任意列表（元素可能是 dataclass / dict / 其它）。

        Returns:
            统一成 list，dataclass 用 asdict，其它用 str 兜底。
        """
        if not items:
            return []
        result: list = []
        for item in items:
            if is_dataclass(item):
                result.append(asdict(item))
            elif isinstance(item, dict):
                result.append(item)
            else:
                result.append(str(item))
        return result

    @staticmethod
    def _serialize_msg(msg: Any) -> dict:
        """把单条消息压成可序列化 dict。

        Args:
            msg: BaseMessage 或类似对象。

        Returns:
            {type, content, id, tool_calls, tool_call_id, name}
        """
        return {
            "type": type(msg).__name__,
            "content": msg.content if isinstance(msg.content, str) else str(msg.content),
            "id": getattr(msg, "id", None),
            "tool_calls": getattr(msg, "tool_calls", None) or None,
            "tool_call_id": getattr(msg, "tool_call_id", None),
            "name": getattr(msg, "name", None),
        }

    def _write_to_disk(self, snapshot: dict) -> str | None:
        """把快照写盘。

        文件名：``snapshot-<YYYYmmdd_HHMMSS>.json``。

        Args:
            snapshot: 快照 dict。

        Returns:
            绝对路径；失败返回 None。
        """
        try:
            abs_dir = os.path.abspath(self._dir)
            os.makedirs(abs_dir, exist_ok=True)
            ts = datetime.now(CST).strftime("%Y%m%d_%H%M%S")
            filename = f"snapshot-{ts}.json"
            filepath = os.path.join(abs_dir, filename)
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2, default=str)
            return filepath
        except (OSError, TypeError) as exc:
            logger.error("snapshot write failed: %s (dir=%s, type=%s)", exc, self._dir, type(exc).__name__)
            return None