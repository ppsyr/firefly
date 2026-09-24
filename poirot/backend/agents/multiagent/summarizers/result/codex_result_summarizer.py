"""CodexResultSummarizer — Codex 输出端转换器（测试通过率 + diff 合法性）。

【整体职责】
在 BaseResultSummarizer 通用校验之上，为 Codex specialist 追加两项编码场景
专属校验：若输出报告了测试结果，"0 passed" 视为失败；并在 gap_analysis 中
补充测试失败数或 diff 标记缺失说明。

【内容摘要】
- _TEST_RESULT_RE / _TEST_FAIL_RE / _DIFF_MARKER_RE : 测试通过 / 失败 / diff 标记正则。
- CodexResultSummarizer      : 输出端转换器主类。
- _evaluate_success()        : 覆写基类成功判定，追加"0 passed → 失败"规则。
- _parse_test_pass_rate()    : 从输出解析通过测试数（无则 None）。
- _extract_gap()             : 覆写基类 gap 提取，追加测试失败 / diff 缺失说明。

【职责边界】
- 只负责：在基类校验之上追加 Codex 专属的成功判定与 gap 补充。
- 不负责：通用校验逻辑（基类负责）、结果压缩（基类负责）、specialist 执行。
- 不持有运行时状态：纯函数式判定。

【INVARIANT】
- 继承 BaseResultSummarizer，specialist_name 固定 "codex"。
- 成功判定叠加：基类判定通过 **且** 未出现"0 passed"才算成功。
- 仅在能解析出测试通过数（passed is not None）时才有"0 passed → 失败"规则；
  无法解析测试结果时不做额外判定（避免误判非测试类任务）。
- 测试通过数匹配 _TEST_RESULT_RE（"<n> passed"，大小写不敏感）。
- 测试失败数匹配 _TEST_FAIL_RE（"<n> failed"），仅用于 gap 补充，不参与成功判定。
- diff 标记匹配 _DIFF_MARKER_RE（@@ / --- / +++ 行首），仅用于 gap 补充。
- gap_analysis 在基类基础上追加：
  - 有 "N failed" → 附 "Tests failed: N"
  - 无 diff 标记 → 附 "No diff markers found in output."
- programmatic eval floor 由基类保证，本类不改变其基本语义。
"""
from __future__ import annotations

import re

from poirot.backend.agents.multiagent.summarizers.result.base import (
    BaseResultSummarizer,
)
from poirot.backend.agents.multiagent.types import ArtifactRef

# 测试通过数正则：匹配 "<n> passed"（大小写不敏感）。
_TEST_RESULT_RE = re.compile(r"(\d+)\s+passed", re.IGNORECASE)
# 测试失败数正则：匹配 "<n> failed"（大小写不敏感）。
_TEST_FAIL_RE = re.compile(r"(\d+)\s+failed", re.IGNORECASE)
# diff 标记正则：匹配 @@ / --- / +++ 行首。
_DIFF_MARKER_RE = re.compile(r"^@@|^---|^\+\+\+", re.MULTILINE)


class CodexResultSummarizer(BaseResultSummarizer):
    """Codex specialist 输出端转换器（测试通过率 + diff 合法性）。"""

    def __init__(self) -> None:
        """初始化，固定 specialist_name="codex"。"""
        super().__init__(specialist_name="codex")

    def _evaluate_success(
        self,
        raw_output: str,
        artifacts: list[ArtifactRef],
        success_criteria: str,
    ) -> bool:
        """成功判定：基类通过 且 未出现"0 passed"，才算成功。

        仅当能解析出测试通过数时才追加此规则；无法解析时不做额外判定。
        """
        if not super()._evaluate_success(raw_output, artifacts, success_criteria):
            return False
        passed = self._parse_test_pass_rate(raw_output)
        if passed is not None and passed == 0:
            return False
        return True

    def _parse_test_pass_rate(self, raw_output: str) -> int | None:
        """从输出解析通过测试数（匹配 _TEST_RESULT_RE）；无匹配返回 None。"""
        match = _TEST_RESULT_RE.search(raw_output)
        if match:
            return int(match.group(1))
        return None

    def _extract_gap(self, raw_output: str, success_criteria: str) -> str:
        """gap 提取：基类结果之上追加测试失败 / diff 缺失说明。

        - 有 "<n> failed" → 附 "Tests failed: n"
        - 无 diff 标记 → 附 "No diff markers found in output."
        """
        base_gap = super()._extract_gap(raw_output, success_criteria)
        failed = _TEST_FAIL_RE.search(raw_output)
        if failed:
            return f"{base_gap}. Tests failed: {failed.group(1)}"
        if not _DIFF_MARKER_RE.search(raw_output):
            return f"{base_gap}. No diff markers found in output."
        return base_gap