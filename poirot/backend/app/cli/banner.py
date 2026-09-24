"""Poirot CLI banner — rich 渲染，冷色系渐变（cyan→blue→purple）。

【整体职责】
渲染 CLI / TUI 的启动横幅：冷色系渐变的 ASCII logo（像素字体）+ 版本号 + tagline。
用 rich Text + Style 构造，供 console.print() 渲染，不再手写 ANSI 码。

【内容摘要】
- _GRADIENT      : 冷色系渐变色表（6 行逐行变色）。
- _VERSION_COLOR : 版本号/tagline 颜色。
- _PIXEL_POIROT  : POIROT 像素字体 ASCII art（6 行）。
- render_logo    : 仅返回渐变 logo（不含版本/tagline），供 TUI 居中展示。
- render_banner  : 返回完整横幅（logo + 版本 + tagline）。

【职责边界】
- 只负责：构造并返回 rich Text 横幅。
- 不负责：打印（由调用方 console.print）、TUI 布局（tui 模块）、
  其他交互文案（welcome.md 等）。

【INVARIANT】
- 纯 rich Text：不使用手写 ANSI 码，避免与 rich Console 冲突。
- 逐行着色：logo 每行从 _GRADIENT 循环取色（idx % len）。
- 行尾不留多余空格：便于 width: auto 容器精确居中。
- 非 "POIROT" 文本：退化为单行大写着色 + 换行。
- 输出为 Text 对象：可被 console.print / Textual 容器消费。
"""
from __future__ import annotations

from rich.text import Text

# 冷色系渐变：cyan → blue → indigo → purple，6 行逐行变色
_GRADIENT = [
    "#00CED1",  # dark turquoise
    "#00BFFF",  # deep sky blue
    "#1E90FF",  # dodger blue
    "#4169E1",  # royal blue
    "#6A5ACD",  # slate blue
    "#8A2BE2",  # blue violet
]
_VERSION_COLOR = "#48D1CC"  # medium turquoise

# POIROT in box-drawing pixel font, 6 rows.
_PIXEL_POIROT = [
    "██████╗  ██████╗ ██╗██████╗  ██████╗ ████████╗",
    "██╔══██╗██╔═══██╗██║██╔══██╗██╔═══██╗╚══██╔══╝",
    "██████╔╝██║   ██║██║██████╔╝██║   ██║   ██║   ",
    "██╔═══╝ ██║   ██║██║██╔══██╗██║   ██║   ██║   ",
    "██║     ╚██████╔╝██║██║  ██║╚██████╔╝   ██║   ",
    "╚═╝      ╚═════╝ ╚═╝╚═╝  ╚═╝ ╚═════╝    ╚═╝   ",
]


def render_logo() -> Text:
    """仅返回渐变 ASCII logo（不含版本/tagline），供 TUI 居中展示。

    每行独立着色，行尾不留多余空格，便于 ``width: auto`` 容器精确居中。

    Returns:
        Text: 居中对齐的渐变 logo。
    """
    result = Text(justify="center")
    for idx, row in enumerate(_PIXEL_POIROT):
        color = _GRADIENT[idx % len(_GRADIENT)]
        result.append(row, style=color)
        if idx < len(_PIXEL_POIROT) - 1:
            result.append("\n")
    return result


def render_banner(text: str = "POIROT") -> Text:
    """返回 rich Text 对象，供 console.print() 渲染。

    冷色系渐变 ASCII art + 版本号 + tagline。不用手写 ANSI 码。

    Args:
        text: 横幅文本；为 "POIROT" 时渲染像素字体，否则单行大写着色。

    Returns:
        Text: 完整横幅（logo + 版本 + tagline）。
    """
    result = Text()

    upper = text.upper()
    if upper == "POIROT":
        for idx, row in enumerate(_PIXEL_POIROT):
            color = _GRADIENT[idx % len(_GRADIENT)]
            result.append(row, style=color)
            result.append("\n")
    else:
        result.append(upper, style=_GRADIENT[0])
        result.append("\n")

    result.append("\n")
    result.append("v1.0.0  |  ", style=_VERSION_COLOR)
    result.append("The little grey cells are working...", style="italic " + _VERSION_COLOR)

    return result