---

name: Poirot CLI banner — rich 渲染，冷色系渐变（cyan→blue→purple）。

description:

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

---

## 0. 结构树

### 0.1. 静态结构树

```
banner.py — Poirot CLI 横幅（rich 渲染，冷色系渐变）
│
├─ _GRADIENT: list[str]                       ← 冷色系渐变色表（6 色）
│   ├─ "#00CED1"  dark turquoise
│   ├─ "#00BFFF"  deep sky blue
│   ├─ "#1E90FF"  dodger blue
│   ├─ "#4169E1"  royal blue
│   ├─ "#6A5ACD"  slate blue
│   └─ "#8A2BE2"  blue violet
│
├─ _VERSION_COLOR: str = "#48D1CC"            ← 版本/tagline 颜色
│
├─ _PIXEL_POIROT: list[str]                   ← POIROT 像素字体（6 行）
│
├─ render_logo() -> Text                      ← 仅 logo（供 TUI 居中）
│   ├─ Text(justify="center")
│   └─ 逐行 append(row, style=color)           ← 行尾不加多余空格
│
└─ render_banner(text="POIROT") -> Text       ← 完整横幅（logo + 版本 + tagline）
    ├─ text.upper() == "POIROT"？
    │   ├─ 是 → 逐行渲染 _PIXEL_POIROT（渐变着色）
    │   └─ 否 → 单行大写着色 + 换行
    ├─ 空行
    └─ "v1.0.0  |  " + "The little grey cells are working..."
        （_VERSION_COLOR + italic）
```

---

### 0.2. 渲染组成视图

```
render_banner("POIROT") 输出（Text 对象）
│
├─ 像素 logo（6 行，逐行渐变）
│   ├─ 行 1 → _GRADIENT[0]  #00CED1
│   ├─ 行 2 → _GRADIENT[1]  #00BFFF
│   ├─ 行 3 → _GRADIENT[2]  #1E90FF
│   ├─ 行 4 → _GRADIENT[3]  #4169E1
│   ├─ 行 5 → _GRADIENT[4]  #6A5ACD
│   └─ 行 6 → _GRADIENT[5]  #8A2BE2
│
├─ 空行
└─ "v1.0.0  |  The little grey cells are working..."（_VERSION_COLOR + italic）

render_logo() 输出
└─ 仅上面的像素 logo 部分（justify="center"）
```

---

### 0.3. 运行时调用树

```
① CLI 启动（_run_chat_async 初始化）
main._run_chat_async
    └─ console.print(render_banner("POIROT"))
          ├─ text.upper() == "POIROT" → 逐行渲染 _PIXEL_POIROT
          │     └─ 每行 append(row, style=_GRADIENT[idx % 6])
          ├─ append("\n")
          └─ append("v1.0.0  |  " + tagline, style=_VERSION_COLOR)
    └─ rich Console 渲染为终端输出

② TUI 启动（PoirotTUI 使用）
app.tui（或 tui 组件）
    └─ render_logo()
          └─ Text(justify="center") + 逐行着色
                └─ 交给 Textual 容器（width: auto 精确居中）

③ 其他文案
    └─ （welcome.md 等由 prompts 模块单独加载，与本文件无关）
```

---

### 0.4. 补充

**`banner.py` 是 CLI/TUI 的视觉横幅——用 rich Text 构造冷色系渐变的像素 logo 与版本 tagline，提供 `render_logo`（仅 logo，供 TUI）与 `render_banner`（完整横幅，供 CLI）两个出口。**

几条主线：

- **纯 rich Text，不手写 ANSI**：注释明确"改用 rich Text + Style，避免与 rich Console 冲突"——这是对旧 colorama 实现的替换。
- **两个渲染出口**：
  - `render_logo()`——仅 logo 且 `justify="center"`，供 TUI 精确居中（行尾不留空格是关键，否则居中会偏）。
  - `render_banner()`——完整横幅（logo + 版本 + tagline），供 CLI `console.print`。
- **逐行渐变**：logo 6 行、色表 6 色，`idx % len(_GRADIENT)` 保证行数与色数不匹配时仍安全循环。
- **文本参数化**：`render_banner(text)` 仅当 `upper == "POIROT"` 时渲染像素字体，否则退化为单行大写着色——支持未来换名字而不必改像素图。
- **硬编码版本号**：`"v1.0.0"` 直接写在函数里（与 `main.py` 的 `_print_status` 里的版本号重复出现）。
- **与 welcome 的分工**：本文件是**静态视觉横幅**（不调 LLM、不读文件）；`welcome.md` 是**开场白文案**（由 prompts 模块加载、`main.py` 的 `_print_welcome` 消费）——两者在 CLI 启动时先后打印，职责不同。