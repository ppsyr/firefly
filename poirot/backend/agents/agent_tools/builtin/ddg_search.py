"""DuckDuckGo 网络搜索工具 —— 直调 ddgs 库。

【整体职责】
提供「无 API key、无 Node 中间层」的网页搜索能力：
直接调用 ddgs 库完成搜索，结果规范化为 JSON 返回。
定位上优先于 freeweb-mcp 的 web_search 使用，以降低失败率。
（设计来源：借鉴 deer-flow 的 ddg_search 模块。）

【组成】
1. 模块级默认参数（常量）：
   - DEFAULT_MAX_RESULTS ：默认返回条数（5）。
   - DEFAULT_REGION      ：搜索地区（"wt-wt"，全球）。
   - DEFAULT_SAFESEARCH  ：安全搜索级别（"moderate"）。
   - DEFAULT_BACKEND     ：后端标识（"duckduckgo"）。
2. 模块级 logger：用于记录搜索失败。
3. 唯一对外工具：web_search_tool(query, max_results)
   - 注册名为 "web_search"，供 LLM 调用。

【返回格式】
统一 JSON 字符串：
   - 成功：{"query", "total_results", "results":[{"title","url","content"}]}
   - 失败：{"error", "query"}

【职责边界】
- 只负责「搜索 + 结果规范化 + 序列化」，不做结果抓取/正文解析。
- 两类失败（ddgs 未安装 / 搜索抛异常 / 无结果）都转为 JSON error 返回，不向上抛。
- 依赖 ddgs 为惰性导入（函数内 import），未安装时不影响模块导入。

【备注】
- 工具名是 "web_search"（非函数名 web_search_tool），与 agent_tools 分组白名单里的
  "web_search"（core 组）一致。
"""

from __future__ import annotations

import json
import logging

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# 搜索默认参数（模块级常量，供工具函数默认值使用）
DEFAULT_MAX_RESULTS = 5
DEFAULT_REGION = "wt-wt"           # 全球（wt-wt = world-wide）
DEFAULT_SAFESEARCH = "moderate"    # 安全搜索级别
DEFAULT_BACKEND = "duckduckgo"     # 使用的搜索后端标识


@tool("web_search", parse_docstring=True)
def web_search_tool(query: str, max_results: int = DEFAULT_MAX_RESULTS) -> str:
    """Search the web for information. Use this tool to find current information, news, articles, and facts from the internet.

    Args:
        query: Search keywords describing what you want to find. Be specific for better results.
        max_results: Maximum number of results to return. Default is 5.
    """
    # 惰性导入 ddgs；未安装则直接返回 error JSON（不抛异常）
    try:
        from ddgs import DDGS
    except ImportError:
        return json.dumps({"error": "ddgs library not installed. Run: pip install ddgs", "query": query}, ensure_ascii=False)

    # 构造 DDGS 客户端，超时 30s
    ddgs = DDGS(timeout=30)
    try:
        # 执行搜索：地区/安全级别/条数/后端均取默认常量
        results = ddgs.text(
            query,
            region=DEFAULT_REGION,
            safesearch=DEFAULT_SAFESEARCH,
            max_results=max_results,
            backend=DEFAULT_BACKEND,
        )
        # ddgs 可能返回生成器，统一转 list；None 视为空列表
        results = list(results) if results else []
    except Exception as e:
        # 搜索异常：记 error 日志 + 返回 error JSON
        logger.error("DuckDuckGo search failed: %s", e)
        return json.dumps({"error": f"Search failed: {e}", "query": query}, ensure_ascii=False)

    # 无结果：返回 error JSON
    if not results:
        return json.dumps({"error": "No results found", "query": query}, ensure_ascii=False)

    # 规范化每条结果：字段名统一为 title / url / content
    # （url 兼容 href/link；content 兼容 body/snippet）
    normalized = [
        {
            "title": r.get("title", ""),
            "url": r.get("href", r.get("link", "")),
            "content": r.get("body", r.get("snippet", "")),
        }
        for r in results
    ]
    # 输出 JSON：查询词 + 结果数 + 规范化结果，缩进 2，不转义非 ASCII
    return json.dumps({"query": query, "total_results": len(normalized), "results": normalized}, indent=2, ensure_ascii=False)