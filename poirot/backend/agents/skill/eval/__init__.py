"""Skill eval 评估层包（L3 装配容器）。

【整体职责】
定义 L3 评估层的组件容器 EvalLayer，把评估相关的组件聚合为一个不可变对象，
由 bootstrap 装配后注入 SkillManager（set_eval_layer）。
它是"评估层"对外暴露的入口对象，持有四类评估组件。

【内容摘要】
- EvalLayer(frozen) : L3 eval 组件容器。
    - bridge            : RegistryEvalBridge，评估注册桥接（组件与注册表的连接层）。
    - judgment_analyzer : SkillJudgmentAnalyzer | None，技能判断分析器。
    - task_judge        : TaskQualityJudge | None，任务质量评判器。
    - runtime_tracker   : RuntimeTracker，运行时追踪器（窗口内表现）。

【职责边界】
- 只负责：声明评估层组件容器（聚合引用）。
- 不负责：评估逻辑本身（在 analyzers / tracker / bridge 各自模块里）。
- 不负责装配：由 bootstrap 构造后通过 set_eval_layer 注入 SkillManager。
- 不持有可变状态：frozen，构造后字段不可改。

【INVARIANT】
- 全部 frozen（不可变值对象）。
- bridge 与 runtime_tracker 为必需组件（非 Optional）。
- judgment_analyzer / task_judge 可为 None（对应 SkillEvalConfig 的开关关闭）。
- 由 bootstrap 装配，经 SkillManager.set_eval_layer 回注（避免循环依赖）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EvalLayer:
    """L3 eval 组件容器，bootstrap 装配后注入 SkillManager。

    字段：
    - bridge            : RegistryEvalBridge，评估注册桥接。
    - judgment_analyzer : SkillJudgmentAnalyzer | None，技能判断分析器
                          （对应 SkillEvalConfig.judgment_enabled）。
    - task_judge        : TaskQualityJudge | None，任务质量评判器
                          （对应 SkillEvalConfig.task_judge_enabled）。
    - runtime_tracker   : RuntimeTracker，运行时追踪器（窗口内表现追踪）。
    """
    bridge: Any                      # RegistryEvalBridge
    judgment_analyzer: Any | None    # SkillJudgmentAnalyzer
    task_judge: Any | None           # TaskQualityJudge
    runtime_tracker: Any             # RuntimeTracker