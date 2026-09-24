"""Sandbox 系统 — Agent 代码/文件操作的执行边界。

【整体职责】
方案 C 组合模式：Sandbox 具体类组合三个可替换组件，负责编排
validate → translate → execute → mask 四步流程，把 Agent 的代码/文件操作
约束在一个受控边界内。Sandbox 是具体类（非 ABC）——有真实编排逻辑。

【组合的三个组件】
- SandboxRuntime      : 裸执行——真正跑命令 / 读写文件。不知路径翻译、不做安全检查；
                        异构差异（本地 / 容器）封装在各自实现里，异常统一为 SandboxError 子类。
- PathTranslator      : 路径翻译——在虚拟路径 ↔ 真实路径之间双向映射；
                        并承担输出脱敏（mask_output 是路径翻译的逆操作）。
- SecurityGuard       : 安全校验——只管 validate_path / validate_command，
                        不做脱敏（脱敏归 PathTranslator）。

【Sandbox 自身的编排职责】
按固定顺序调度三组件，形成四步流程：
    guard.validate → translator.translate → runtime.execute → translator.mask

【核心约束（Stage 1）】
- 编排顺序固定：guard.validate → translator.translate → runtime.execute → translator.mask。
- mask_output 归 PathTranslator；Guard 只做 validate。
- SandboxRuntime 只裸执行，异常统一为 SandboxError 子类。
- merge_sandbox 幂等：同 id 合并 OK，不同 id fail-closed 抛 ValueError；无主动清空语义。
- SandboxInfo.created_at 用 utc_now_iso，与 journal 统一。

详见 design_docs/25-sandbox-overview.md + 26-sandbox-stage1-core-abstraction.md。
"""