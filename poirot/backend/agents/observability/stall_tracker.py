"""StallTracker — 检测 agent 是否陷入死胡同。

【整体职责】
通过四类信号判断 agent 是否停滞（stuck）：
1. 能力耗尽：同一 capability 经 5 次不同命令尝试仍失败。
2. 错误模式重复：同一错误类别反复出现 5 次。
3. 待办停滞：同一 todo 连续 15 轮 LLM 仍处 in_progress。
4. 长操作无进展：工具运行 180s 无新输出（依赖 RunActivityTracker 心跳，Phase 3 接入）。

三个主动信号在最近 120 秒内有过成功工具调用时被抑制（_success_decay_window），
避免长时间编码任务中"瞬时错误与真实进展并存"导致误报。

tracker 跨 run 无状态；help 请求解决后 reset() 清空所有信号，让 agent 重新开始。

【内容摘要】
- ToolFailure            : 单次工具失败记录。
- _CAPABILITY_PATTERNS   : 能力分类正则表。
- _ERROR_CLASS_PATTERNS  : 错误类别正则表。
- classify_capability    : 按 tool/input/error 文本归类 capability。
- classify_error_class   : 按 error 文本归类错误类别。
- StallTracker           : 停滞追踪器，记录失败/成功/待办/进展并判定 stuck。
- StallTracker.stuck / get_stuck_reason / reset 等。

【职责边界】
- 只负责：记录信号、判定停滞、给出停滞原因、重置。
- 不负责：活动生命周期追踪（RunActivityTracker）、停滞的处置与介入（interrupt_protection /
  HITL 中间件）、信号的采集来源（工具调用方 / todo 中间件）。
  第 4 类信号（no-progress）所需心跳来自 RunActivityTracker，本模块仅预留阈值。

【INVARIANT】
- 三类主动信号：capability 耗尽 / error 模式重复 / todo 停滞，任一成立即 stuck。
- 成功衰减窗口：最近 _success_decay_window（120s）内有成功则三类信号均被抑制。
- 初始不抑制：_last_success_ts 初始为 0（epoch），"从未成功过"时 _recent_success() 为 False。
- 能力信号按"不同命令数"计：同一 capability 下 command 去重后 >= 阈值才判定耗尽。
- unknown 不计入：capability 为 "unknown" 的失败不参与能力耗尽判定。
- 无进展信号未接入：no_progress_timeout 已定义，但第 4 类信号待 Phase 3 接入心跳。
- reset 语义：清空全部信号，_last_success_ts 归 0（与构造时一致）。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolFailure:
    """单次工具失败记录。

    Attributes:
        capability: 能力分类（sandbox / postgres / root / docker / network / unknown）。
        error_class: 错误类别（permission / sandbox / network / not_found / unknown）。
        command: 命令或输入摘要（截断）。
        error: 错误信息（截断）。
        timestamp: 发生时间戳。
    """

    capability: str
    error_class: str
    command: str
    error: str
    timestamp: float


_CAPABILITY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("sandbox", re.compile(r"winerror|sandbox|exec failed|write failed|read failed|aio_sandbox|agent_sandbox", re.IGNORECASE)),
    ("postgres", re.compile(r"postgres|psql|pg_isready|pgvector", re.IGNORECASE)),
    ("root", re.compile(r"apt-get|apt |dpkg|sudo|/var/lib/apt", re.IGNORECASE)),
    ("docker", re.compile(r"docker|docker-compose|containerd", re.IGNORECASE)),
    ("network", re.compile(r"curl|wget|http://|https://|504|gateway.{0,10}timeout", re.IGNORECASE)),
]

_ERROR_CLASS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("permission", re.compile(r"permission denied|eacces|403|forbidden", re.IGNORECASE)),
    ("sandbox", re.compile(r"winerror|sandbox.{0,20}(fail|error|refus)|exec failed|write failed|read failed", re.IGNORECASE)),
    ("network", re.compile(r"timeout|504|gateway.{0,10}timeout|connection refused|unreachable", re.IGNORECASE)),
    ("not_found", re.compile(r"not found|no such file|404|not installed|absent", re.IGNORECASE)),
]


def classify_capability(tool_name: str, tool_input: dict[str, Any], error: str) -> str:
    """按 tool 名 / 输入 / 错误文本归类 capability。

    Args:
        tool_name: 工具名。
        tool_input: 工具输入。
        error: 错误信息。

    Returns:
        str: 命中的 capability 名；未命中返回 "unknown"。
    """
    text = f"{tool_name} {tool_input} {error}"
    for cap, pattern in _CAPABILITY_PATTERNS:
        if pattern.search(text):
            return cap
    return "unknown"


def classify_error_class(error: str) -> str:
    """按错误文本归类错误类别。

    Args:
        error: 错误信息。

    Returns:
        str: 命中的错误类别；未命中返回 "unknown"。
    """
    for cls, pattern in _ERROR_CLASS_PATTERNS:
        if pattern.search(error):
            return cls
    return "unknown"


@dataclass
class StallTracker:
    """停滞追踪器。

    Attributes:
        capability_failure_threshold: 能力耗尽阈值（不同命令数）。
        error_pattern_threshold: 错误模式重复阈值。
        todo_stagnation_rounds: 待办停滞轮数阈值。
        no_progress_timeout: 长操作无进展超时（秒，第 4 类信号预留）。
        _failures: 工具失败记录列表。
        _error_counts: 各错误类别计数。
        _todo_in_progress_hash: 当前 in_progress 待办的哈希。
        _todo_stagnation_count: 待办停滞累计轮数。
        _last_progress_ts: 最近进展时间戳。
        _last_success_ts: 最近成功时间戳（0 表示从未成功）。
        _success_decay_window: 成功衰减窗口（秒）。
    """

    capability_failure_threshold: int = 5
    error_pattern_threshold: int = 5
    todo_stagnation_rounds: int = 15
    no_progress_timeout: float = 180.0

    _failures: list[ToolFailure] = field(default_factory=list)
    _error_counts: dict[str, int] = field(default_factory=dict)
    _todo_in_progress_hash: str | None = None
    _todo_stagnation_count: int = 0
    _last_progress_ts: float = field(default_factory=time.time)
    # Timestamp of last successful tool call — used to decay stale failure signals.
    # 初始化为 0（epoch）让"从未成功过"时 _recent_success() 返 False（不抑制 stuck 信号）。
    # 只有显式调 record_tool_success() 后才设为 time.time()。
    _last_success_ts: float = 0.0
    # Window (seconds) within which a success resets capability failure tracking.
    _success_decay_window: float = 120.0

    @property
    def stuck(self) -> bool:
        """是否停滞：三类主动信号任一成立。"""
        return self._capability_stuck() or self._error_pattern_stuck() or self._todo_stuck()

    def _recent_success(self) -> bool:
        """True if a successful tool call happened within the decay window."""
        return (time.time() - self._last_success_ts) < self._success_decay_window

    def _capability_stuck(self) -> bool:
        """能力耗尽判定：任一 capability 的不同命令数达阈值。"""
        if self._recent_success():
            return False
        caps: dict[str, set[str]] = {}
        for f in self._failures:
            if f.capability == "unknown":
                continue
            caps.setdefault(f.capability, set()).add(f.command)
        return any(len(cmds) >= self.capability_failure_threshold for cmds in caps.values())

    def _error_pattern_stuck(self) -> bool:
        """错误模式重复判定：任一错误类别计数达阈值。"""
        if self._recent_success():
            return False
        return any(count >= self.error_pattern_threshold for count in self._error_counts.values())

    def _todo_stuck(self) -> bool:
        """待办停滞判定：同一 in_progress 待办连续轮数达阈值。"""
        if self._recent_success():
            return False
        return self._todo_stagnation_count >= self.todo_stagnation_rounds

    def record_tool_failure(self, tool_name: str, tool_input: dict[str, Any], error: str) -> None:
        """记录一次工具失败，并累加对应错误类别计数。

        Args:
            tool_name: 工具名。
            tool_input: 工具输入。
            error: 错误信息。
        """
        cap = classify_capability(tool_name, tool_input, error)
        cls = classify_error_class(error)
        cmd = str(tool_input.get("command", tool_input))[:200]
        self._failures.append(ToolFailure(cap, cls, cmd, error[:500], time.time()))
        self._error_counts[cls] = self._error_counts.get(cls, 0) + 1

    def record_tool_success(self) -> None:
        """Call after any successful tool execution to decay stale failure signals."""
        self._last_success_ts = time.time()
        self._last_progress_ts = time.time()

    def record_todo_state(self, todos: list[dict[str, Any]]) -> None:
        """记录待办状态，更新 in_progress 待办的停滞计数。

        同一 in_progress 组合连续出现则累加；变化则重置（空则归 0）。

        Args:
            todos: 待办列表。
        """
        in_progress = sorted(
            t.get("content", "") for t in todos if t.get("status") == "in_progress"
        )
        current_hash = "|".join(in_progress)
        if current_hash == self._todo_in_progress_hash and current_hash:
            self._todo_stagnation_count += 1
        else:
            self._todo_stagnation_count = 1 if current_hash else 0
            self._todo_in_progress_hash = current_hash or None

    def record_progress(self) -> None:
        """记录一次进展：刷新进展与成功时间戳。"""
        self._last_progress_ts = time.time()
        self._last_success_ts = time.time()

    def get_failures(self) -> list[ToolFailure]:
        """返回全部工具失败记录（副本）。"""
        return list(self._failures)

    def get_stuck_reason(self) -> str | None:
        """返回停滞原因描述；未停滞返回 None。"""
        if self._capability_stuck():
            caps = {}
            for f in self._failures:
                caps.setdefault(f.capability, set()).add(f.command)
            exhausted = [c for c, cmds in caps.items() if len(cmds) >= self.capability_failure_threshold]
            return f"capability exhausted: {', '.join(exhausted)}"
        if self._error_pattern_stuck():
            exceeded = [c for c, n in self._error_counts.items() if n >= self.error_pattern_threshold]
            return f"error pattern repeated: {', '.join(exceeded)}"
        if self._todo_stuck():
            return f"todo stagnated for {self._todo_stagnation_count} rounds"
        return None

    def reset(self) -> None:
        """清空全部信号，重置成功时间戳为 0（与构造时一致）。"""
        self._failures.clear()
        self._error_counts.clear()
        self._todo_in_progress_hash = None
        self._todo_stagnation_count = 0
        self._last_progress_ts = time.time()
        # reset 后 _last_success_ts=0，_recent_success() 返 False（不抑制新的失败信号）。
        # 与构造时一致：从未成功过 = 不 decay。
        self._last_success_ts = 0.0