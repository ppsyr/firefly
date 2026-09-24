"""status_bar — prompt_toolkit bottom_toolbar 渲染。

【整体职责】
提供纯函数 build_bottom_toolbar：读 cli_state 中的 mode / model / token / fraction
等字段，返回 prompt_toolkit HTML 供 PromptSession(bottom_toolbar=...) 调用。
每次 UI 刷新都会被调用，天然支持实时更新。

【内容摘要】
- build_bottom_toolbar : 渲染常驻状态栏，返回 HTML。

【职责边界】
- 只负责：把 cli_state 渲染为 bottom_toolbar 的 HTML。
- 不负责：cli_state 的更新（main 主循环 / StreamRenderer）、状态栏的布局与显示
  （prompt_toolkit）、其他 UI 元素（banner / spinner）。

【INVARIANT】
- 纯函数：只读 cli_state，不产生副作用，不修改入参。
- 缺省安全：mode / model / tokens / fraction 缺失时回退安全默认值。
- 格式固定：`{mode} · {model} | {tokens}K ({fraction}%) | {activity}/help`。
- 运行中指示：_running 且 _current_activity 非空时插入 " ● {activity} "。
- 颜色固定：前景 #aaaaaa、背景 #2b2b2b。
- 与 rich 输出不冲突：bottom_toolbar 占终端最后一行；流式期间 rich Live 接管，
  prompt_async 期间 bottom_toolbar 激活，两者不重叠。
"""
from __future__ import annotations

from typing import Any

from prompt_toolkit.formatted_text import HTML


def build_bottom_toolbar(cli_state: dict[str, Any]) -> HTML:
    """渲染常驻状态栏：``mode · model | {tokens}K ({fraction}%) | /help``。

    Args:
        cli_state: 主循环持有的状态 dict，需含 ``mode``/``model``/``current_tokens``/
            ``current_fraction`` 四个 key（缺省时回退到安全默认值）。
            可选 ``_running`` / ``_current_activity`` 用于运行中活动指示。

    Returns:
        HTML: prompt_toolkit HTML 对象，供 ``bottom_toolbar`` callable 返回。
    """
    mode = cli_state.get("mode", "default")
    model = cli_state.get("model", "?")
    tokens = cli_state.get("current_tokens", 0)
    fraction = cli_state.get("current_fraction", 0.0)
    running = cli_state.get("_running", False)
    activity = cli_state.get("_current_activity", "")

    tokens_k = tokens / 1000.0
    pct = fraction * 100.0

    activity_part = f" ● {activity} " if running and activity else ""
    return HTML(
        f'<style fg="#aaaaaa" bg="#2b2b2b">'
        f" {mode} · {model} "
        f"</style>"
        f'<style fg="#aaaaaa" bg="#2b2b2b">'
        f"| {tokens_k:.1f}K ({pct:.1f}%) |{activity_part}/help"
        f"</style>"
    )