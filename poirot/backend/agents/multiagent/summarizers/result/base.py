"""BaseResultSummarizer — 通用 programmatic eval floor。

【整体职责】
为所有 specialist 提供输出端转换的通用骨架：压缩 raw_output、程序化校验
success_criteria、提取 gap_analysis、分类 failure_category，产出 SpecialistResult。
子类通过覆写钩子方法（_evaluate_success / _extract_gap 等）加入专属校验。

【内容摘要】
- _MAX_SUMMARY_CHARS / _SENSITIVE_PATTERNS : 压缩上限 + 敏感改动正则。
- BaseResultSummarizer                    : 通用骨架主类。
- summarize()                             : 对外唯一入口，编排校验 / 压缩 / gap / 分类。
- _classify_failure()                     : 启发式失败分类（供 L2 FailureFocuser 读取）。
- _evaluate_success()                     : 通用成功判定（钩子，子类可覆写）。
- _has_sensitive_changes()                : 检测敏感改动（钩子）。
- _compress()                             : 压缩 raw_output（钩子）。
- _extract_gap()                          : 提取失败原因（钩子，子类可覆写）。

【职责边界】
- 只负责：通用校验、压缩、gap 提取、失败分类，产出 SpecialistResult。
- 不负责：per-specialist 专属校验（子类覆写钩子实现）、specialist 执行、
  上下文摘要（ContextSummarizer 负责）。
- 不持有运行时状态：specialist_name 只用于产出结果时填字段。

【INVARIANT】
- 对外唯一入口是 summarize；其余以下划线开头的方法都是钩子，供子类覆写。
- 不回传 raw_output 全量：超过 _MAX_SUMMARY_CHARS=2000 时截断 + "...(truncated)"。
- 成功判定三条通用规则（全部满足才成功）：
  1. raw_output 非空白；
  2. 无敏感改动（匹配 _SENSITIVE_PATTERNS）；
  3. artifacts 非空（None 视为不检查）。
- gap_analysis 仅在 success=False 时提取。
- failure_category 仅 success=False 时非 None，走启发式分类。
- specialist_name 默认 "base"，子类构造时传入自己的名字。
- 子类覆写 _evaluate_success / _extract_gap 时必须先调 super()，保持通用校验叠加。
"""
from __future__ import annotations

import re
from typing import Any

from poirot.backend.agents.multiagent.types import (
    ArtifactRef,
    SpecialistResult,
)

# 压缩上限：超过此字符数截断。
_MAX_SUMMARY_CHARS = 2000
# 敏感改动正则：命中即视为失败，防止危险命令出现在输出中。
_SENSITIVE_PATTERNS = [
    r"rm\s+-rf\s+/",
    r"mkfs\.",
    r"dd\s+of=/dev/",
    r">\s*/etc/passwd",
    r"chmod\s+777\s+/",
]


class BaseResultSummarizer:
    """通用 programmatic eval floor。

    对外只暴露 summarize()；其余 _ 开头方法为钩子，供 per-specialist 子类覆写。
    """

    def __init__(self, specialist_name: str = "base") -> None:
        """初始化。specialist_name 会写进产出的 SpecialistResult。"""
        self._specialist_name = specialist_name

    def summarize(
        self,
        raw_output: str,
        artifacts: list[ArtifactRef],
        goal: str,
        success_criteria: str,
    ) -> SpecialistResult:
        """对外唯一入口：编排校验 → 压缩 → gap → 分类 → 产出 SpecialistResult。

        流程：
        1. _evaluate_success 判定成败。
        2. success=False 时 _extract_gap 提 gap；否则 gap 为空字符串。
        3. _compress 压缩 raw_output。
        4. _classify_failure 分类失败（success=True 时为 None）。
        5. 组装 SpecialistResult。
        """
        success = self._evaluate_success(raw_output, artifacts, success_criteria)
        gap_analysis = "" if success else self._extract_gap(raw_output, success_criteria)
        summary = self._compress(raw_output)
        failure_category = self._classify_failure(success, raw_output)

        return SpecialistResult(
            specialist_name=self._specialist_name,
            summary=summary,
            artifacts=tuple(artifacts),
            success=success,
            gap_analysis=gap_analysis,
            failure_category=failure_category,
        )

    def _classify_failure(self, success: bool, raw_output: str) -> str | None:
        """启发式失败分类，供 L2 FailureFocuser 读取。

        - success=True → None
        - success=False 时按关键词匹配：
          - 含 "context"          → "context_insufficient"
          - 含 "skill" / "ability"→ "ability_insufficient"
          - 含 "goal" / "unclear" → "goal_unclear"
          - 含 "sandbox" / "timeout" → "sandbox_issue"
          - 默认                  → "ability_insufficient"
        """
        if success:
            return None
        lower = raw_output.lower()
        if "context" in lower:
            return "context_insufficient"
        if "skill" in lower or "ability" in lower:
            return "ability_insufficient"
        if "goal" in lower or "unclear" in lower:
            return "goal_unclear"
        if "sandbox" in lower or "timeout" in lower:
            return "sandbox_issue"
        return "ability_insufficient"

    def _evaluate_success(
        self,
        raw_output: str,
        artifacts: list[ArtifactRef],
        success_criteria: str,
    ) -> bool:
        """通用成功判定（钩子，子类可覆写）。

        三条规则全部满足才成功：
        1. raw_output 非空白；
        2. 无敏感改动；
        3. artifacts 非空（None 视为不检查）。
        """
        if not raw_output.strip():
            return False
        if self._has_sensitive_changes(raw_output):
            return False
        if artifacts is not None and len(artifacts) == 0:
            return False
        return True

    def _has_sensitive_changes(self, raw_output: str) -> bool:
        """检测输出是否包含敏感改动（匹配 _SENSITIVE_PATTERNS）。"""
        for pattern in _SENSITIVE_PATTERNS:
            if re.search(pattern, raw_output):
                return True
        return False

    def _compress(self, raw_output: str) -> str:
        """压缩 raw_output：超过 _MAX_SUMMARY_CHARS 时截断 + "...(truncated)"。"""
        if len(raw_output) <= _MAX_SUMMARY_CHARS:
            return raw_output
        return raw_output[:_MAX_SUMMARY_CHARS] + "\n...(truncated)"

    def _extract_gap(self, raw_output: str, success_criteria: str) -> str:
        """提取失败原因（钩子，子类可覆写）。

        格式："Criteria not met: <success_criteria>. Output tail: <最后 5 行，最多 200 字符>"
        """
        lines = raw_output.strip().split("\n")
        last_lines = lines[-5:] if len(lines) >= 5 else lines
        return f"Criteria not met: {success_criteria}. Output tail: {' '.join(last_lines)[:200]}"