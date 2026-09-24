"""Prompt 管理系统包 — 提示词加载 / 渲染 / 切换。

【整体职责】
作为提示词系统的对外入口，暴露统一管理能力：从 .md 文件加载 prompt、
按 ${variable} 渲染、支持 user/ 覆盖 system/。按三层架构组织，
本包为 low-level 文件访问层的出口，上层经 accessor 消费。

【内容摘要】
- manager          : 核心管理模块，唯一直接访问 prompt 文件的层。
- PromptManager    : 统一加载 / 渲染 / 切换管理类。
- get_prompt_manager : 全局单例访问器。
- system/          : 系统提示词目录（按角色 / 用途分目录，.md 文本）。
- user/            : 用户覆盖目录（同名 prompt 优先于 system/）。

【职责边界】
- 只负责：提示词文件的加载、缓存、变量渲染、列出与 user/system 切换。
- 不负责：知道具体 category/name 与变量语义（模块 accessor 层）、
  业务级 prompt 组合（外部调用方）、prompt 内容编写（.md 文件本身）。

【三层架构】
- low-level ：manager（本包），唯一碰文件。
- mid-level ：模块 accessor（如 leader/prompts.py），知道 category/name + vars。
- high-level：外部调用方，只调 accessor。

【INVARIANT】
- prompt 持久化为 .md 文件。
- 渲染语法 ${variable} regex 替换；未绑定变量保留原样 + stderr warning。
- user/ 覆盖 system/。
"""
from poirot.backend.agents.prompts.manager import PromptManager, get_prompt_manager

__all__ = ["PromptManager", "get_prompt_manager"]