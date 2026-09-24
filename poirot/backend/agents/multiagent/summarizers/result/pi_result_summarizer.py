"""PiResultSummarizer — Pi 输出端转换器（三段解析 + programmatic eval floor）。

【整体职责】
在 BaseResultSummarizer 通用校验之上，为 Pi specialist 追加"三段输出格式"解析：
从输出中提取 What You Did / Success / Gaps 三段，用 Success 段的内容参与成功判定，
用 Gaps 段作为 gap_analysis，用 What You Did 段作为 summary。

【内容摘要】
- _WHAT_YOU_DID_RE / _SUCCESS_SECTION_RE / _GAPS_SECTION_RE : 三段标题正则。
- _SUCCESS_POSITIVE_RE / _SUCCESS_NEGATIVE_RE              : Success 段正/负面关键词。
- PiResultSummarizer          : 输出端转换器主类。
- summarize()                 : 覆写基类入口，编排三段解析 + 成功判定 + 结果组装。
- _parse_pi_sections()        : 从输出解析三段（缺失段返空串）。
- _evaluate_pi_success()      : pi-specific 成功判定（base 通过 + Success 段无负面）。

【职责边界】
- 只负责：解析三段格式、在基类校验之上追加 pi 专属成功判定、优先用三段内容组装结果。
- 不负责：通用校验逻辑（基类负责）、结果压缩（基类提供兜底）、specialist 执行。
- 不持有运行时状态：纯函数式判定。

【INVARIANT】
- 继承 BaseResultSummarizer，specialist_name 固定 "pi"。
- 覆写 summarize（而非只覆写钩子）：因为要插入"三段解析"这一额外步骤。
- 三段正则与 PiRuntime._build_prompt 的 Output Format 保持对齐。
- 成功判定叠加：base 通用校验通过 **且** Success 段无负面关键词。
- 无 Success 段时退化为 base 判定（不做额外否定）。
- 负面关键词：no / fail / not met / partial / incomplete。
- 正面关键词：yes / pass / met / complete（当前仅定义，未参与判定）。
- summary 优先用 What You Did 段；缺失时用基类 _compress 兜底。
- gap_analysis 优先用 Gaps 段；缺失且失败时用基类 _extract_gap 兜底。
- 所有正则大小写不敏感（re.IGNORECASE），三段用 re.DOTALL 跨行匹配。
- 不传回 raw output 全量：summary 走三段或基类截断。
"""
from __future__ import annotations

import re

from poirot.backend.agents.multiagent.summarizers.result.base import (
    BaseResultSummarizer,
)
from poirot.backend.agents.multiagent.types import ArtifactRef

# 三段输出格式正则（与 PiRuntime._build_prompt 的 Output Format 对齐）。
# What You Did 段：从 "## What You Did" 到下一个 "## Success" / "## Gaps" / 结尾。
_WHAT_YOU_DID_RE = re.compile(
    r"##\s*What You Did\s*\n(.*?)(?=##\s*Success|##\s*Gaps|$)",
    re.DOTALL | re.IGNORECASE,
)
# Success 段：从 "## Success" 到下一个 "## Gaps" / 结尾。
_SUCCESS_SECTION_RE = re.compile(
    r"##\s*Success\s*\n(.*?)(?=##\s*Gaps|$)",
    re.DOTALL | re.IGNORECASE,
)
# Gaps 段：从 "## Gaps" 到结尾。
_GAPS_SECTION_RE = re.compile(
    r"##\s*Gaps\s*\n(.*?)$",
    re.DOTALL | re.IGNORECASE,
)
# Success 段正面关键词（yes / pass / met / complete）。
_SUCCESS_POSITIVE_RE = re.compile(r"\b(yes|pass|met|complete)\b", re.IGNORECASE)
# Success 段负面关键词（no / fail / not met / partial / incomplete）。
_SUCCESS_NEGATIVE_RE = re.compile(r"\b(no|fail|not met|partial|incomplete)\b", re.IGNORECASE)


class PiResultSummarizer(BaseResultSummarizer):
    """Pi specialist 输出端转换器（三段解析 + programmatic eval floor）。

    继承基类通用校验；Pi-specific 扩展：解析 _build_prompt 要求的三段输出格式。
    """

    def __init__(self) -> None:
        """初始化，固定 specialist_name="pi"。"""
        super().__init__(specialist_name="pi")

    def summarize(
        self,
        raw_output: str,
        artifacts: list[ArtifactRef],
        goal: str,
        success_criteria: str,
    ) -> "SpecialistResult":  # type: ignore[name-defined]
        """覆写基类入口：三段解析 + 成功判定 + 结果组装。

        流程：
        1. base_success = 基类通用校验结果。
        2. sections = _parse_pi_sections(raw_output)。
        3. pi_success = _evaluate_pi_success(sections, base_success)。
        4. gap：优先 Gaps 段；缺失且失败时用基类 _extract_gap 兜底。
        5. summary：优先 What You Did 段；缺失时用基类 _compress 兜底。
        6. 组装 SpecialistResult（不填 failure_category）。
        """
        # 调 base 通用校验（产物存在性 + success_criteria 回应 + 无敏感改动）
        base_success = self._evaluate_success(raw_output, artifacts, success_criteria)

        # 解析 pi 输出的三段
        sections = self._parse_pi_sections(raw_output)

        # pi-specific success 判定：base 校验通过 + Success 段无负面关键词
        pi_success = self._evaluate_pi_success(sections, base_success)

        # gap_analysis：优先用 Gaps 段，否则用 base 提取
        gap = sections.get("gaps", "") or (
            "" if pi_success else self._extract_gap(raw_output, success_criteria)
        )

        # summary：优先用 What You Did 段，否则用 base 压缩
        summary = sections.get("did", "") or self._compress(raw_output)

        from poirot.backend.agents.multiagent.types import SpecialistResult

        return SpecialistResult(
            specialist_name=self._specialist_name,
            summary=summary,
            artifacts=tuple(artifacts),
            success=pi_success,
            gap_analysis=gap,
        )

    def _parse_pi_sections(self, output: str) -> dict[str, str]:
        """解析 pi 输出的 What You Did / Success / Gaps 三段。

        按 _build_prompt 要求的格式提取三段内容；缺失段不出现在返回 dict 中。
        返回 dict 可能含键：did / success / gaps。
        """
        sections: dict[str, str] = {}

        did_match = _WHAT_YOU_DID_RE.search(output)
        if did_match:
            sections["did"] = did_match.group(1).strip()

        success_match = _SUCCESS_SECTION_RE.search(output)
        if success_match:
            sections["success"] = success_match.group(1).strip()

        gaps_match = _GAPS_SECTION_RE.search(output)
        if gaps_match:
            sections["gaps"] = gaps_match.group(1).strip()

        return sections

    def _evaluate_pi_success(
        self, sections: dict[str, str], base_success: bool
    ) -> bool:
        """pi-specific 成功判定。

        判定逻辑：
        1. base 通用校验必须通过（产物存在 + success_criteria 回应 + 无敏感改动）。
        2. Success 段无负面关键词（no / fail / not met / partial / incomplete）。
        3. 无 Success 段时退化为 base 判定。
        """
        if not base_success:
            return False

        success_section = sections.get("success", "").lower()
        if not success_section:
            # 无 Success 段，依赖 base 判定
            return base_success

        # 有负面关键词 → 失败
        if _SUCCESS_NEGATIVE_RE.search(success_section):
            return False

        # 无负面关键词 → 通过（base 已校验）
        return True