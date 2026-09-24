"""Skill 配置层 — .env 读取 + frozen dataclass。

【整体职责】
定义 skill 子系统的配置对象，并从环境变量（POIROT_SKILL_*）加载配置。
配置为不可变（frozen dataclass），供 SkillManager / selector / evolution / eval 读取。

【内容摘要】
- _PROJECT_ROOT              ：项目根路径（parents[4]），用于把相对路径锚定到项目根。
- _anchor(path)              ：把相对路径锚到项目根，绝对路径原样返回。
- SkillConfig                ：skill 顶层配置（enabled / 存储 / 选择阈值 / 进化 / hub）。
- SkillEvalConfig            ：Layer 3 eval 配置（判分 / 契约检查 / 窗口 / 权重等）。
- load_skill_config()        ：从 os.environ 读取并构造 SkillConfig。

【职责边界】
- 只负责：配置字段定义 + 环境变量读取 + 路径锚定 + 类型转换。
- 不负责：配置的校验之外的行为、skill 的发现 / 选择 / 打点 / 进化 / 评估逻辑。
- 不做运行期热更新：每次 load_skill_config() 重新从 env 读取。

【INVARIANT】
- 所有 POIROT_SKILL_ENABLED 缺省 false（opt-in，与 MCP 一致）；
  false 时 build_skill_manager 返 None，不影响既有行为。
- db_path 缺省 .poirot/skills.db，相对项目根（锚定后 CWD 无关）。
- skill_dirs 缺省 ("skills/",)，环境变量以逗号分隔多个目录。
- max_inject 缺省 3，单轮最多注入 skill 数。
- quality_threshold 缺省 0.3，quality filter 淘汰阈值。
- min_selections 缺省 5，淘汰判定最少 selections（anti-loop，给新 skill 积累数据的机会）。
- int / float 转换失败 → 用默认值，不抛异常。
- 相对路径统一经 _anchor 锚定到项目根，避免从非项目根启动时误跳过。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# 项目根：config.py 位于 poirot/backend/agents/skill/，parents[4] 即项目根。
# db_path / skill_dirs 的默认值为相对路径，锚定到项目根后与 CWD 无关，
# 避免从非项目根启动时因找不到 skills/ 目录或 .env 而被误跳过
# （与 logs_root 同款处理）。
_PROJECT_ROOT = Path(__file__).parents[4]


def _anchor(path: str) -> str:
    """相对路径锚定到项目根；绝对路径原样返回。

    Args:
        path: 待处理的路径字符串。

    Returns:
        绝对路径字符串：相对路径拼到 _PROJECT_ROOT 后 resolve，
        绝对路径直接返回原值。
    """
    p = Path(path)
    if p.is_absolute():
        return path
    return str((_PROJECT_ROOT / p).resolve())


@dataclass(frozen=True)
class SkillConfig:
    """Skill 模块顶层配置（不可变）。

    基础开关与存储：
    - enabled            : 是否启用 skill 模块（缺省 false；false 时
                           build_skill_manager 返 None）。
    - db_path            : SQLite 路径（相对项目根，构造前已锚定）。
    - skill_dirs         : skill 扫描目录元组。
    - include_builtin    : 是否加载内置 core 技能。

    选择与淘汰（供 SkillSelector）：
    - max_inject         : 单轮最多注入 skill 数。
    - quality_threshold  : quality filter 淘汰阈值
                           （effective_rate < threshold 且 selections >= min 时淘汰）。
    - min_selections     : 淘汰判定最少 selections（anti-loop，给新 skill 数据积累机会）。

    自进化（Layer 2a，默认 false opt-in）：
    - evolve_enabled          : 是否启用自进化。
    - evolve_threshold        : 触发进化的分数阈值。
    - evolve_min_selections   : 触发进化的最少 selections。
    - evolve_cooldown_turns   : 两次进化之间的冷却轮数。
    - evolve_mutate_budget    : 变异预算。
    - evolve_max_steps        : 单次进化最大步数。

    Layer 3 eval：
    - eval_config        : SkillEvalConfig，评估配置。

    Skill Hub（H8，默认 true opt-in）：
    - hub_enabled             : 是否启用技能中心。
    - hub_quarantine_enabled  : 是否启用隔离区。
    - hub_audit_log           : 是否记录审计日志。
    """
    enabled: bool = False
    db_path: str = ".poirot/skills.db"
    skill_dirs: tuple[str, ...] = ("skills/",)
    include_builtin: bool = True
    max_inject: int = 3
    quality_threshold: float = 0.3
    min_selections: int = 5
    # 自进化（Layer 2a，默认 false opt-in）
    evolve_enabled: bool = False
    evolve_threshold: float = 0.3
    evolve_min_selections: int = 5
    evolve_cooldown_turns: int = 10
    evolve_mutate_budget: int = 20
    evolve_max_steps: int = 5
    eval_config: "SkillEvalConfig" = field(default_factory=lambda: SkillEvalConfig())
    # Skill Hub 配置（H8，默认 true opt-in）
    hub_enabled: bool = True
    hub_quarantine_enabled: bool = True
    hub_audit_log: bool = True


@dataclass(frozen=True)
class SkillEvalConfig:
    """Layer 3 eval 配置（D-L3-8，默认 opt-in false）。

    开关：
    - enabled            : eval 总开关。
    - judgment_enabled   : 启用技能判断分析。
    - task_judge_enabled : 启用任务质量评判。
    - contract_check     : 启用响应契约检查。
    - async_eval         : 是否异步评估。
    - skip_no_skill      : 无技能时跳过评估。

    参数：
    - runtime_window     : 运行时追踪窗口大小。
    - degradation_delta  : 退化判定增量阈值。
    - captured_min_score : 捕获所需最低分。
    - max_messages_chars : 送入评估的最大消息字符数。
    - task_weights       : 任务评分权重元组。
    """
    enabled: bool = False
    judgment_enabled: bool = True
    task_judge_enabled: bool = True
    contract_check: bool = True
    async_eval: bool = True
    skip_no_skill: bool = True
    runtime_window: int = 20
    degradation_delta: float = 0.15
    captured_min_score: float = 0.5
    max_messages_chars: int = 80000
    task_weights: tuple[float, ...] = (0.50, 0.35, 0.05, 0.10)


def load_skill_config() -> SkillConfig:
    """从 os.environ 读取 POIROT_SKILL_* 构造 SkillConfig，缺省用默认值。

    转换规则：int / float 转换失败时使用默认值，不抛异常
    （保证坏的环境变量不会阻断启动）。

    Returns:
        SkillConfig：由环境变量与默认值组装而成的不可变配置。
    """
    enabled = os.environ.get("POIROT_SKILL_ENABLED", "false").lower() == "true"
    db_path = _anchor(os.environ.get("POIROT_SKILL_DB_PATH", ".poirot/skills.db"))
    include_builtin = os.environ.get("POIROT_SKILL_INCLUDE_BUILTIN", "true").lower() == "true"
    evolve_enabled = os.environ.get("POIROT_SKILL_EVOLVE_ENABLED", "false").lower() == "true"
    # H8: hub 配置（默认 true opt-in）
    hub_enabled = os.environ.get("POIROT_SKILL_HUB_ENABLED", "true").lower() == "true"
    hub_quarantine = os.environ.get("POIROT_SKILL_HUB_QUARANTINE", "true").lower() == "true"
    hub_audit = os.environ.get("POIROT_SKILL_HUB_AUDIT", "true").lower() == "true"

    dirs_raw = os.environ.get("POIROT_SKILL_DIRS", "")
    if dirs_raw:
        skill_dirs = tuple(_anchor(d.strip()) for d in dirs_raw.split(",") if d.strip())
    else:
        skill_dirs = (_anchor("skills"),)

    try:
        max_inject = int(os.environ.get("POIROT_SKILL_MAX_INJECT", "3"))
    except (ValueError, TypeError):
        max_inject = 3

    try:
        quality_threshold = float(os.environ.get("POIROT_SKILL_QUALITY_THRESHOLD", "0.3"))
    except (ValueError, TypeError):
        quality_threshold = 0.3

    try:
        min_selections = int(os.environ.get("POIROT_SKILL_MIN_SELECTIONS", "5"))
    except (ValueError, TypeError):
        min_selections = 5

    try:
        evolve_threshold = float(os.environ.get("POIROT_SKILL_EVOLVE_THRESHOLD", "0.3"))
    except (ValueError, TypeError):
        evolve_threshold = 0.3

    try:
        evolve_min_selections = int(os.environ.get("POIROT_SKILL_EVOLVE_MIN_SELECTIONS", "5"))
    except (ValueError, TypeError):
        evolve_min_selections = 5

    try:
        evolve_cooldown_turns = int(os.environ.get("POIROT_SKILL_EVOLVE_COOLDOWN_TURNS", "10"))
    except (ValueError, TypeError):
        evolve_cooldown_turns = 10

    try:
        evolve_mutate_budget = int(os.environ.get("POIROT_SKILL_EVOLVE_MUTATE_BUDGET", "20"))
    except (ValueError, TypeError):
        evolve_mutate_budget = 20

    try:
        evolve_max_steps = int(os.environ.get("POIROT_SKILL_EVOLVE_MAX_STEPS", "5"))
    except (ValueError, TypeError):
        evolve_max_steps = 5

    # Layer 3 eval 配置
    eval_enabled = os.environ.get("POIROT_SKILL_EVAL_ENABLED", "false").lower() == "true"
    eval_judgment = os.environ.get("POIROT_SKILL_EVAL_JUDGMENT_ENABLED", "true").lower() == "true"
    eval_task_judge = os.environ.get("POIROT_SKILL_EVAL_TASK_JUDGE_ENABLED", "true").lower() == "true"
    eval_contract = os.environ.get("POIROT_SKILL_EVAL_CONTRACT_CHECK", "true").lower() == "true"
    eval_async = os.environ.get("POIROT_SKILL_EVAL_ASYNC", "true").lower() == "true"
    eval_skip_no_skill = os.environ.get("POIROT_SKILL_EVAL_SKIP_NO_SKILL", "true").lower() == "true"

    try:
        eval_window = int(os.environ.get("POIROT_SKILL_EVAL_RUNTIME_WINDOW", "20"))
    except (ValueError, TypeError):
        eval_window = 20

    try:
        eval_degradation = float(os.environ.get("POIROT_SKILL_EVAL_DEGRADATION_DELTA", "0.15"))
    except (ValueError, TypeError):
        eval_degradation = 0.15

    try:
        eval_captured_min = float(os.environ.get("POIROT_SKILL_EVAL_CAPTURED_MIN_SCORE", "0.5"))
    except (ValueError, TypeError):
        eval_captured_min = 0.5

    try:
        eval_max_chars = int(os.environ.get("POIROT_SKILL_EVAL_MAX_MESSAGES_CHARS", "80000"))
    except (ValueError, TypeError):
        eval_max_chars = 80000

    weights_raw = os.environ.get("POIROT_SKILL_EVAL_TASK_WEIGHTS", "0.50,0.35,0.05,0.10")
    try:
        eval_weights = tuple(float(w.strip()) for w in weights_raw.split(","))
    except (ValueError, TypeError):
        eval_weights = (0.50, 0.35, 0.05, 0.10)

    eval_config = SkillEvalConfig(
        enabled=eval_enabled,
        judgment_enabled=eval_judgment,
        task_judge_enabled=eval_task_judge,
        contract_check=eval_contract,
        async_eval=eval_async,
        skip_no_skill=eval_skip_no_skill,
        runtime_window=eval_window,
        degradation_delta=eval_degradation,
        captured_min_score=eval_captured_min,
        max_messages_chars=eval_max_chars,
        task_weights=eval_weights,
    )

    return SkillConfig(
        enabled=enabled,
        db_path=db_path,
        skill_dirs=skill_dirs,
        include_builtin=include_builtin,
        max_inject=max_inject,
        quality_threshold=quality_threshold,
        min_selections=min_selections,
        evolve_enabled=evolve_enabled,
        evolve_threshold=evolve_threshold,
        evolve_min_selections=evolve_min_selections,
        evolve_cooldown_turns=evolve_cooldown_turns,
        evolve_mutate_budget=evolve_mutate_budget,
        evolve_max_steps=evolve_max_steps,
        eval_config=eval_config,
        hub_enabled=hub_enabled,
        hub_quarantine_enabled=hub_quarantine,
        hub_audit_log=hub_audit,
    )