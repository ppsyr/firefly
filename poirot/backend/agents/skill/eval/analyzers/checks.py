"""Skill eval 确定性检查函数 — 纯函数，无类，无循环依赖。

【整体职责】
提供 eval 层的所有"确定性检查"能力（纯函数），是检查逻辑的单一真相源。
被 ResponseContractChecker 与 ProgrammaticEvalBridge（facade）共同复用；
check 逻辑只在本模块定义，其他模块不重复实现。

【内容摘要】
模块常量：
- HARD_MODES        : 硬失败模式（触发即 reject 倾向）：nonempty / json_parseable。
- DIRECTIVE_WORDS   : 指令性词（用于 semantic_density 计算）。
- UNFOUNDED_WORDS   : 无据绝对化词（用于 no_unfounded_claims）。
- CONCLUSION_WORDS  : 结论词（用于 lead_with_conclusion）。
- CITE_PATTERN      : 引用标记正则（URL / @mention / 来源词）。
- YAML_FRONTMATTER  : frontmatter 正则（---\\n{yaml}\\n---\\n）。
- PARAGRAPH_LIMIT   : 段落数上限（默认 20）。
- SEMANTIC_DENSITY_MIN / MAX : 指令性词密度的合理区间。

纯函数：
- read_content(record)             : 读 SKILL.md 全文；失败返 ""。
- split_body(content)              : 去 frontmatter，返 body。
- check_nonempty(content)          : body 非空。
- check_json_parseable(content)    : frontmatter YAML 可解析（无 frontmatter 也算 pass）。
- check_must_cite(content)         : 含引用标记。
- check_paragraph_limit(content)   : 段落数 <= 上限。
- check_lead_with_conclusion(content): 前 3 段含结论词。
- check_no_unfounded_claims(content): 无绝对化无据词。
- semantic_density(content)        : 指令性词密度（供区间判定）。

【职责边界】
- 只负责：确定性检查 + 文本读取与切分。
- 不负责：规则编译（contract_compiler）、规则分发与打分（response_contract_checker）、
  LLM 评估（analyzer / judge）、持久化（store）。
- 无状态、无类、无副作用（除读文件）。

【INVARIANT】
- 全部为纯函数（read_content 除外，它做文件 IO）。
- 无循环依赖：本模块不 import eval 内部其他模块。
- 单一真相源：所有 check 逻辑只在此定义，被 checker / bridge 复用。
- 检查失败/异常不抛：read_content 失败返 ""，check 函数返 bool。
"""
from __future__ import annotations

import re
from pathlib import Path

# hard failure modes（关键失败，触发即 reject 倾向）
HARD_MODES = ("nonempty", "json_parseable")

# 指令性词（semantic_density，借鉴 SkillOpt）
DIRECTIVE_WORDS = ("MUST", "ALWAYS", "NEVER", "SHOULD", "MUST NOT", "REQUIRED", "FORBIDDEN")
UNFOUNDED_WORDS = ("绝对", "一定", "必然", "毫无疑问", "absolutely", "definitely", "certainly")
CONCLUSION_WORDS = ("结论", "总结", "核心", "要点", "conclusion", "summary", "key")
CITE_PATTERN = re.compile(r"(https?://|@[\w-]+|来源|引用|source|cite)", re.IGNORECASE)
YAML_FRONTMATTER = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)

PARAGRAPH_LIMIT = 20
SEMANTIC_DENSITY_MIN = 0.005
SEMANTIC_DENSITY_MAX = 0.15


def read_content(record) -> str:
    """读 SKILL.md 全文（record.path 指向文件）。

    失败（文件不存在 / 权限等）→ 返回空串，不抛异常。

    Args:
        record: 含 path 字段的技能记录。

    Returns:
        文件全文；读失败返回 ""。
    """
    try:
        return Path(record.path).read_text(encoding="utf-8")
    except Exception:
        return ""


def split_body(content: str) -> str:
    """去掉 frontmatter，返回正文 body。

    有 frontmatter 时取 `---` 之后的部分；无 frontmatter 时原样返回。

    Args:
        content: SKILL.md 全文。

    Returns:
        正文 body。
    """
    m = YAML_FRONTMATTER.match(content)
    if m:
        return content[m.end():]
    return content


def check_nonempty(content: str) -> bool:
    """body（去 frontmatter 后）非空 → True。"""
    return len(split_body(content).strip()) > 0


def check_json_parseable(content: str) -> bool:
    """frontmatter YAML 可解析 → True。

    无 frontmatter 也算 pass（返回 True）。
    YAML 解析失败 → False。
    """
    m = YAML_FRONTMATTER.match(content)
    if not m:
        return True
    try:
        import yaml
        yaml.safe_load(m.group(1))
        return True
    except Exception:
        return False


def check_must_cite(content: str) -> bool:
    """含引用标记（URL / @mention / 来源词）→ True。"""
    return bool(CITE_PATTERN.search(content))


def check_paragraph_limit(content: str) -> bool:
    """body 段落数 <= PARAGRAPH_LIMIT（20）→ True。

    段落按空行分隔，空白段落不计。
    """
    body = split_body(content)
    paragraphs = [p for p in body.split("\n\n") if p.strip()]
    return len(paragraphs) <= PARAGRAPH_LIMIT


def check_lead_with_conclusion(content: str) -> bool:
    """前 3 段含结论词 → True。

    body 为空 → False。大小写不敏感。
    """
    body = split_body(content).strip()
    if not body:
        return False
    paras = body.split("\n\n")[:3]
    head = "\n\n".join(paras)
    return any(w.lower() in head.lower() for w in CONCLUSION_WORDS)


def check_no_unfounded_claims(content: str) -> bool:
    """不含绝对化无据词 → True（含则 False）。"""
    return not any(w in content for w in UNFOUNDED_WORDS)


def semantic_density(content: str) -> float:
    """指令性词密度 = 指令词出现次数 / 总词数。

    - content 为空 → 0.0。
    - 无词（\w+ 匹配不到）→ 0.0。
    - 指令词按大写计数（content.upper().count(w)）。
    """
    if not content:
        return 0.0
    words = re.findall(r"\w+", content)
    if not words:
        return 0.0
    count = sum(content.upper().count(w) for w in DIRECTIVE_WORDS)
    return count / len(words)