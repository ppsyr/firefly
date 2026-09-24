"""MCP 工具标记工具（给工具打上 MCP 来源标记，并据此判定）。

【整体职责】
为「工具是否来自 MCP」提供统一的打标与判定能力。
做法：往 BaseTool.metadata 里写入一个约定 key（MCP_TOOL_METADATA_KEY），
后续任何模块都能通过该 key 判断某工具是否来自 MCP，无需依赖工具名或类型。

【组成】
1. 标记 key（模块级常量）：
   - MCP_TOOL_METADATA_KEY：写入/读取 metadata 时使用的约定 key，值为 "poirot_mcp"。
2. 打标函数：
   - tag_mcp_tool(tool)：把 MCP 标记写入工具的 metadata，并返回该工具。
3. 判定函数：
   - is_mcp_tool(tool)：读取 metadata 中的标记，返回布尔值。

【职责边界】
- 本模块只负责「打标」与「判定」两件事，不负责 MCP 工具的加载、注册或调用。
- 标记存放位置是 tool.metadata（dict），不改动工具的其他属性。
- tag_mcp_tool 会就地写入并返回同一对象（便于链式使用），不是复制。
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

# MCP 工具的 metadata 标记 key。
# 用途：在 tool.metadata 中以该 key 记录「此工具来自 MCP」，供 is_mcp_tool 判定。
MCP_TOOL_METADATA_KEY = "poirot_mcp"


def tag_mcp_tool(tool: BaseTool) -> BaseTool:
    """给工具打上「来自 MCP」的标记，并返回该工具。
    """
    tool.metadata = {**(tool.metadata or {}), MCP_TOOL_METADATA_KEY: True}
    return tool


def is_mcp_tool(tool: BaseTool) -> bool:
    """判断某工具是否被标记为「来自 MCP」。
    """
    return bool((tool.metadata or {}).get(MCP_TOOL_METADATA_KEY))