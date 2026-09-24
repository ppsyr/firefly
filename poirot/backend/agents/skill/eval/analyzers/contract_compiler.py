"""ContractCompiler — 从 skill 文本自动编译 contract 规则（AutoSkill EvalCompiler 模型）。

【整体职责】
响应层 eval 的"规则编译器"：读取 skill 文本，自动决定要跑哪些 contract 规则，
产出一份 list[ContractRule] 供 ResponseContractChecker 执行。
- 外部 skill 零改造：不要求 frontmatter 声明，扫描 skill 自身文本关键词即可。
- 替代 L2a 的固定 7 mode：规则按 skill 内容动态编译。

【内容摘要】
模块常量：
- _RE_PARAGRAPH_LIMIT : 段落数限制的提取正则（中英文表达 + 数字 + 单位）。

类方法：
- compile(skill_content)      : 编译规则清单（对外主入口）。
- _mentions_sources(corpus)   : 文本是否提到"引用来源"。
- _mentions_conclusion_first(corpus) : 文本是否提到"先给结论"。
- _paragraph_limit(corpus)    : 文本是否声明段落数限制，返回上限值（0 表示未声明）。

【职责边界】
- 只负责：从 skill 文本编译出规则清单（哪些规则要跑）。
- 不负责：规则检查（checks.py）、打分与汇总（ResponseContractChecker）、
  LLM 评估（analyzer / judge）、持久化（store）。
- 不做检查：只产 ContractRule，不执行。

【INVARIANT】
- D-L3-4：contract-aware 替代 L2a 固定 7 mode；外部 skill 零改造。
- 规则层级：
    全局硬规则（始终跑，失败触发 hard_failure）：nonempty + json_parseable
    contract 规则（skill 文本提到才编译，失败不触发 hard_failure）：
        must_cite / lead_with_conclusion / paragraph_limit
    soft 规则（始终跑，不作 hard_failure）：no_unfounded_claims + semantic_density
- _paragraph_limit 返回 0 表示未声明，此时不编译该规则。
- 文本匹配对大小写不敏感（compile 内先 lower）。
- _paragraph_limit 提取的数字至少为 1（max(1, int(...))）。
"""
from __future__ import annotations

import re

from poirot.backend.agents.skill.eval.types import ContractRule

_RE_PARAGRAPH_LIMIT = re.compile(
    r"(不超过|少于|最多|within|less than|at most)\s*(\d+)\s*(段|paragraph)",
    re.IGNORECASE,
)


class ContractCompiler:
    """从 skill 文本编译 contract 规则。

    编译策略：
    - 全局硬规则：始终产（nonempty / json_parseable）。
    - contract 规则：扫 skill 文本关键词，命中才产。
    - soft 规则：始终产，但 hard=False。
    """

    def compile(self, skill_content: str) -> list[ContractRule]:
        """编译规则清单（对外主入口）。

        步骤：
            1. 始终加两条全局硬规则：nonempty / json_parseable（hard=True）。
            2. corpus = skill_content.lower()，用于关键词匹配。
            3. 命中 _mentions_sources → 加 must_cite（hard=False）。
            4. 命中 _mentions_conclusion_first → 加 lead_with_conclusion（hard=False）。
            5. _paragraph_limit > 0 → 加 paragraph_limit（hard=False，params.max）。
            6. 始终加两条 soft 规则：no_unfounded_claims / semantic_density（hard=False）。

        Args:
            skill_content: skill 文本（candidate SKILL.md 全文）。

        Returns:
            list[ContractRule]，顺序固定：硬 → contract → soft。
        """
        rules: list[ContractRule] = [
            ContractRule("nonempty", kind="programmatic", hard=True,
                         description="SKILL.md body 非空"),
            ContractRule("json_parseable", kind="programmatic", hard=True,
                         description="frontmatter YAML 可解析"),
        ]

        corpus = skill_content.lower()
        if self._mentions_sources(corpus):
            rules.append(ContractRule("must_cite", kind="programmatic", hard=False,
                                      description="skill 声明引用来源，SKILL.md 应含引用标记"))
        if self._mentions_conclusion_first(corpus):
            rules.append(ContractRule("lead_with_conclusion", kind="programmatic", hard=False,
                                      description="skill 声明先给结论，首段应含结论词"))
        if self._paragraph_limit(corpus) > 0:
            rules.append(ContractRule("paragraph_limit", kind="programmatic", hard=False,
                                      description="skill 声明段落数限制",
                                      params={"max": self._paragraph_limit(corpus)}))

        # soft 规则（始终跑，不作 hard_failure）
        rules.append(ContractRule("no_unfounded_claims", kind="programmatic", hard=False,
                                  description="无绝对化无据声明"))
        rules.append(ContractRule("semantic_density", kind="programmatic", hard=False,
                                  description="指令性词密度在合理区间"))
        return rules

    # ── 关键词检测（借鉴 AutoSkill evals.py）──────────────

    @staticmethod
    def _mentions_sources(corpus: str) -> bool:
        """文本是否提到"引用来源"（中英文关键词命中即 True）。"""
        return any(k in corpus for k in (
            "引用来源", "标注来源", "注明来源", "cite sources",
            "with sources", "provide sources", "source-backed",
        ))

    @staticmethod
    def _mentions_conclusion_first(corpus: str) -> bool:
        """文本是否提到"先给结论"（中英文关键词命中即 True）。"""
        return any(k in corpus for k in (
            "先给结论", "结论在前", "先说结论",
            "answer first", "lead with the conclusion", "bottom line first",
        ))

    @staticmethod
    def _paragraph_limit(corpus: str) -> int:
        """提取段落数限制。

        用 _RE_PARAGRAPH_LIMIT 匹配形如"不超过 N 段 / within N paragraphs"的表达；
        命中返回上限值（至少 1）；未命中返回 0（表示未声明）。
        """
        for match in _RE_PARAGRAPH_LIMIT.finditer(corpus):
            try:
                return max(1, int(match.group(2)))
            except Exception:
                continue
        return 0