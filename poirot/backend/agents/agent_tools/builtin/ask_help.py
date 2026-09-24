"""ask_help 工具 —— 由 LLM 主动发起的「求助」请求。

【整体职责】
让 LLM 在卡住或需要用户拍板时，能主动调用本工具向用户求助。
真正的「拦截 + 暂停 graph」逻辑不在本工具，而在 HelpRequestMiddleware：
本工具只提供「可被 LLM 调用的入口」，middleware 会在执行前拦截，
并返回 Command(goto=END) 来暂停流程、把问题抛给用户。

【组成】
1. 唯一对外工具：ask_help_tool(question, help_type, context, options)
   - 注册名为 "ask_help"，且 return_direct=True。

【return_direct=True 的含义】
工具被调用后直接返回结果、不把结果再喂回 LLM 继续推理；
配合 middleware 的拦截，实际执行会被接管（暂停 graph）。

【工具返回值】
函数体固定返回一句占位字符串
"Help request processed by HelpRequestMiddleware"。
真正给用户的呈现由 middleware 负责，本函数体不承载业务逻辑。

【职责边界】
- 本工具只声明「可被调用的入口 + 参数契约」，不含拦截/暂停逻辑。
- 参数 help_type 限定为 4 种求助类型（见 Literal）。
"""

from __future__ import annotations

from typing import Literal

from langchain_core.tools import tool


@tool("ask_help", return_direct=True)
def ask_help_tool(
    question: str,
    help_type: Literal["missing_info", "approach_choice", "risk_confirmation", "stuck_report"],
    context: str | None = None,
    options: list[str] | None = None,
) -> str:
    """Ask the user for help when you are stuck or need direction.

    Use this tool when you cannot proceed without user input:
    - **missing_info**: A required detail was not provided (e.g., file path, URL, credential).
    - **approach_choice**: Multiple valid approaches exist and you need user preference.
    - **risk_confirmation**: You are about to perform a potentially risky operation.
    - **stuck_report**: You have tried multiple approaches and all failed; you need guidance.

    After calling this tool, execution will be paused automatically and the
    question will be presented to the user. Wait for the user's response
    before continuing.

    Args:
        question: The help question to ask the user. Be specific and clear.
        help_type: The type of help needed.
        context: Optional background explaining why help is needed.
        options: Optional list of choices for the user to pick from.
    """
    # 占位返回：真正的拦截与暂停由 HelpRequestMiddleware 处理，
    # 本函数体不承载业务逻辑（执行前已被 middleware 接管）。
    return "Help request processed by HelpRequestMiddleware"