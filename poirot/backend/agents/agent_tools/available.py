"""工具装配构建器（builtin 工具加载 + group 过滤）。

【整体职责】
把 builtin 工具加载出来，并可选地按 group 白名单过滤，交给上层 Agent 使用。
本模块是「工具获取」的统一入口，但不负责所有工具来源：
MCP 工具由 McpManager 管理，sandbox 工具由 bootstrap 注入 CapabilityRegistry。

【组成】
1. 工具分组标记（模块级常量，白名单）：
   - CORE_TOOL_NAMES     ：core 组，基础工具，default + expert 都加载（省上下文）。
   - SANDBOX_TOOL_NAMES  ：sandbox 组，沙箱工具
                           （bash / read_file / write_file / list_dir / str_replace / present_files）。
   说明：不在上述两个集合中的工具名，一律归为 deferred（深度工具，仅 expert 加载）。
2. 分组判定：
   - _tool_group(name)   ：工具名 → group 的内部映射器，是过滤逻辑的核心判定。
3. 工具列表处理：
   - dedupe_by_name(tools)     ：按工具名去重，保留首次出现。
   - select_search_tool(tools) ：挑出第一个「搜索类」工具。
4. 对外主入口：
   - get_available_tools(groups, include_mcp)：加载 builtin 工具并按 group 过滤。

【职责边界】
- 本模块只加载 builtin 工具（如 ddg_search、read_snapshot）。
- MCP 工具由 McpManager 管理（bootstrap 通过 build_mcp_manager 加载），不在本模块加载。
- sandbox 工具由 bootstrap 注入 CapabilityRegistry，不在此加载；
  SANDBOX_TOOL_NAMES 在这里仅用于「分组判定」，不触发实际加载。
- include_mcp 为保留参数，仅为向后兼容，当前无任何 MCP 加载逻辑。
"""

from __future__ import annotations

import logging

from langchain_core.tools import BaseTool

from poirot.backend.agents.agent_tools.builtin import get_builtin_tools

logger = logging.getLogger(__name__)

# 内置（builtin）工具的 group 分组标记。
#
# 分组的目的是「按需加载工具，节省上下文」：
#   - core     : 基础工具，default 与 expert 两种模式都加载（体积小、通用性强）
#   - deferred : 深度/低频工具，仅 expert 模式加载
#   - sandbox  : 沙箱工具，由 bootstrap 注入 CapabilityRegistry，不经过本模块加载
#
# 注意：CORE_TOOL_NAMES 与 SANDBOX_TOOL_NAMES 只是「白名单」，
# 不在两个集合中的工具名一律归为 deferred（见 _tool_group）。

# core 组：基础工具，default + expert 都加载（省上下文）
CORE_TOOL_NAMES: set[str] = {"web_search", "browse_page", "read_snapshot", "skill_search"}

# sandbox 组：沙箱工具（bash / 文件读写 / 目录列举 / 字符串替换 / 文件呈现）
SANDBOX_TOOL_NAMES: set[str] = {"bash", "read_file", "write_file", "list_dir", "str_replace", "present_files"}


def _tool_group(name: str) -> str:
    """把工具名映射到所属 group。

    映射规则（按优先级）：
        1. 命中 CORE_TOOL_NAMES     -> "core"
        2. 命中 SANDBOX_TOOL_NAMES  -> "sandbox"
        3. 其余                     -> "deferred"

    Args:
        name: 工具名（BaseTool.name）。

    Returns:
        该工具所属的 group 字符串："core" / "sandbox" / "deferred"。
    """
    if name in CORE_TOOL_NAMES:
        return "core"
    if name in SANDBOX_TOOL_NAMES:
        return "sandbox"
    return "deferred"


def dedupe_by_name(tools: list[BaseTool]) -> list[BaseTool]:
    """按工具名去重，保留首次出现的工具，保持原有顺序。

    用于合并多个来源的工具列表时，避免同名工具重复注入
    导致 prompt / 调用歧义。

    Args:
        tools: 待去重的工具列表。

    Returns:
        去重后的工具列表，顺序为各工具名首次出现的顺序。
    """
    seen: set[str] = set()
    result: list[BaseTool] = []
    for tool in tools:
        if tool.name not in seen:
            seen.add(tool.name)
            result.append(tool)
    return result


def select_search_tool(tools: list[BaseTool]) -> BaseTool | None:
    """从工具列表中挑选一个「搜索类」工具。

    判定方式：工具名（小写）中包含 "search" 即视为搜索工具，
    返回第一个命中的工具。

    Args:
        tools: 候选工具列表。

    Returns:
        第一个名称含 "search" 的工具；若没有则返回 None。
    """
    for tool in tools:
        if "search" in tool.name.lower():
            return tool
    return None


def get_available_tools(
    groups: list[str] | None = None,
    include_mcp: bool = True,
) -> list[BaseTool]:
    """加载 builtin 工具，并可选地按 group 过滤。

    职责边界（重要）：
        - 本函数只负责加载并返回 builtin 工具（如 ddg_search、read_snapshot）。
        - MCP 工具由 McpManager 管理（bootstrap 阶段通过 build_mcp_manager 加载），
          不在这里加载。
        - sandbox 工具由 bootstrap 注入 CapabilityRegistry，也不在这里加载。

    Args:
        groups: group 白名单。
            - None：加载全部 builtin 工具（不做过滤）。
            - 例如 ["core"]：只加载 core 组工具。
        include_mcp: 保留参数，仅为向后兼容。当前无任何 MCP 加载逻辑
            （MCP 工具已交由 mcp_manager 统一管理）。

    Returns:
        去重后的 builtin 工具列表；若指定了 groups，则只包含
        其 group 落在白名单内的工具。

    组装规则：
        1. 先加载全部 builtin 工具并去重：
           get_builtin_tools() -> dedupe_by_name()。
        2. 若 groups 为 None：直接返回全部工具，不做过滤。
        3. 若指定了 groups：
           - 用 _tool_group 判定每个工具的 group，保留落在白名单内的；
           - 被过滤掉的工具名记入 skipped，若非空则打 info 日志。
    """
    tools = get_builtin_tools()
    all_tools = dedupe_by_name(tools)

    if groups is None:
        return all_tools

    groups_set = set(groups)
    filtered = [t for t in all_tools if _tool_group(t.name) in groups_set]
    skipped = [t.name for t in all_tools if _tool_group(t.name) not in groups_set]
    if skipped:
        logger.info("tools filtered out by groups=%s: %s", groups, skipped)
    return filtered