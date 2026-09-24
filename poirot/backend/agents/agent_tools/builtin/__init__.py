"""内置工具（builtin tools）汇总注册。

【整体职责】
把「内置工具」（builtin 工具）集中收集到一个列表并返回，作为工具系统的
基础来源；这些工具优先于 MCP 工具（同名去重时优先保留 builtin）。

【组成】
1. imports：从各 builtin 模块导入 4 个工具对象。
2. 唯一对外函数：get_builtin_tools()
   - 返回内置工具列表：[web_search_tool, read_snapshot, ask_help_tool, skill_search]。

【为什么返回「工具对象」而不是「工具名」】
这些对象是 LangChain BaseTool 实例，可直接交给 Agent 挂载；
去重逻辑（dedupe_by_name）在上层按 tool.name 处理，因此本函数不做去重。

【不在本模块加载的工具】
- sandbox 工具（bash / read_file / write_file / list_dir / str_replace）：
  不在此加载。由 Stage 4 bootstrap 按需注入 —— 当 config.sandbox 配了 provider 时，
  调用 make_sandbox_tools(provider) 构造，注入 registry.tools。
- MCP 工具：由 McpManager 管理（bootstrap 阶段通过 build_mcp_manager 加载）。
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from poirot.backend.agents.agent_tools.builtin.ask_help import ask_help_tool
from poirot.backend.agents.agent_tools.builtin.ddg_search import web_search_tool
from poirot.backend.agents.agent_tools.builtin.read_snapshot import read_snapshot
from poirot.backend.agents.agent_tools.builtin.skill_search import skill_search


def get_builtin_tools() -> list[BaseTool]:
    """返回内置工具列表。

    行为：
        - 收集并返回 4 个 builtin 工具对象：
          web_search_tool（网络搜索）、read_snapshot（读压缩前快照）、
          ask_help_tool（求助）、skill_search（技能搜索）。
        - 不做去重；同名去重由上层 dedupe_by_name 处理，且优先保留 builtin。

    Returns:
        内置工具对象列表（list[BaseTool]），顺序为：
        [web_search_tool, read_snapshot, ask_help_tool, skill_search]。

    """
    return [web_search_tool, read_snapshot, ask_help_tool, skill_search]