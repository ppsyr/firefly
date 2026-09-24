---

name: status_bar — prompt_toolkit bottom_toolbar 渲染。

description:

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

---