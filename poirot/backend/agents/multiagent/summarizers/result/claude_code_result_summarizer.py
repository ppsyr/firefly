"""ClaudeCodeResultSummarizer — Claude Code 输出端转换器（review 完整性 + 修改建议）。

【整体职责】
在 BaseResultSummarizer 通用校验之上，为 Claude Code specialist 追加两项
review 专属校验：输出必须包含修改建议（suggestion）才算成功；若无 issue 或
无 suggestion，在 gap_analysis 中说明缺失项。

【内容摘要】
- _SUGGESTION_RE / _ISSUE_RE      : 修改建议 / 问题描述的正则。
- ClaudeCodeResultSummarizer      : 输出端转换器主类。
- _evaluate_success()             : 覆写基类成功判定，追加 suggestion 检查。
- _has_suggestions()              : 判断输出是否含修改建议。
- _has_issues()                   : 判断输出是否含问题描述。
- _extract_gap()                  : 覆写基类 gap 提取，追加缺失说明。

【职责边界】
- 只负责：在基类校验之上追加 Claude Code 专属的成功判定与 gap 补充。
- 不负责：通用校验逻辑（基类负责）、结果压缩（基类负责）、specialist 执行。
- 不持有运行时状态：纯函数式判定。

【INVARIANT】
- 继承 BaseResultSummarizer，specialist_name 固定 "claude"。
- 成功判定叠加：基类判定通过 **且** 输出含 suggestion，才算成功。
- suggestion 匹配走 _SUGGESTION_RE（suggest / recommend / should / fix /
  change / improve / consider），大小写不敏感。
- issue 匹配走 _ISSUE_RE（issue / problem / bug / concern / risk / warning），
  仅用于 gap 补充，不参与成功判定。
- gap_analysis 在基类基础上追加：
  - 无 suggestion → 附 "No modification suggestions found."
  - 有 suggestion 但无 issue → 附 "No review issues identified."
- programmatic eval floor 由基类保证，本类不改变其基本语义。
"""
from __future__ import annotations

import re

from poirot.backend.agents.multiagent.summarizers.result.base import (
    BaseResultSummarizer,
)
from poirot.backend.agents.multiagent.types import ArtifactRef

# 修改建议关键词正则（大小写不敏感）。
_SUGGESTION_RE = re.compile(
    r"(?:suggest|recommend|should|fix|change|improve|consider)", re.IGNORECASE
)
# 问题描述关键词正则（大小写不敏感）。
_ISSUE_RE = re.compile(r"(?:issue|problem|bug|concern|risk|warning)", re.IGNORECASE)


class ClaudeCodeResultSummarizer(BaseResultSummarizer):
    """Claude Code specialist 输出端转换器（review 完整性 + 修改建议）。"""

    def __init__(self) -> None:
        """初始化，固定 specialist_name="claude"。"""
        super().__init__(specialist_name="claude")

    def _evaluate_success(
        self,
        raw_output: str,
        artifacts: list[ArtifactRef],
        success_criteria: str,
    ) -> bool:
        """成功判定：基类通过 且 输出含修改建议，才算成功。"""
        if not super()._evaluate_success(raw_output, artifacts, success_criteria):
            return False
        if not self._has_suggestions(raw_output):
            return False
        return True

    def _has_suggestions(self, raw_output: str) -> bool:
        """判断输出是否含修改建议（匹配 _SUGGESTION_RE）。"""
        return bool(_SUGGESTION_RE.search(raw_output))

    def _has_issues(self, raw_output: str) -> bool:
        """判断输出是否含问题描述（匹配 _ISSUE_RE）。仅用于 gap 补充。"""
        return bool(_ISSUE_RE.search(raw_output))

    def _extract_gap(self, raw_output: str, success_criteria: str) -> str:
        """gap 提取：基类结果之上追加缺失说明。

        - 无 suggestion → 附 "No modification suggestions found."
        - 有 suggestion 但无 issue → 附 "No review issues identified."
        """
        base_gap = super()._extract_gap(raw_output, success_criteria)
        if not self._has_suggestions(raw_output):
            return f"{base_gap}. No modification suggestions found."
        if not self._has_issues(raw_output):
            return f"{base_gap}. No review issues identified."
        return base_gap