"""IdentityTranslator — 恒等路径翻译器（虚拟路径直传，不做任何翻译）。

【整体职责】
实现 path_translator 契约的"零翻译"版本：三个方法都原样返回输入。
用于虚拟路径与真实路径本来就一致的场景——
Docker bind mount 物理对齐（/mnt/poirot/user-data 挂载到容器同路径），
E2B 沙箱内路径就是虚拟路径，都不需要路径/命令/输出的转换。

【内容摘要】
- IdentityTranslator          : 恒等翻译器类。
    ├─ translate_path    : 虚拟路径 → 真实路径，原样返回。
    ├─ translate_command : 命令翻译，原样返回。
    └─ mask_output       : 输出脱敏/回写，原样返回。

【职责边界】
- 只负责：满足 path_translator 契约（提供三个恒等方法）。
- 不负责：任何实际的路径/命令/输出转换（由 local / docker translator 负责）。
- 无状态：不持有任何字段，所有方法都是 identity 返回。

【INVARIANT】
- 三个方法均恒等：输入 = 输出，不做任何转换、替换、脱敏。
- 无参数、无状态：构造不需参数，可安全共享。
- 使用前提是"虚拟路径 == 真实路径"；一旦两者不一致，必须换用
  LocalPathTranslator 或 DockerPathTranslator，不能用本类。
"""
from __future__ import annotations


class IdentityTranslator:
    """恒等路径翻译器：虚拟路径直传，不做任何翻译。

    Docker bind mount 物理对齐（/mnt/poirot/user-data 挂载到容器同路径），
    E2B 沙箱内路径就是虚拟路径，都不需翻译。
    """

    def translate_path(self, virtual_path: str) -> str:
        """虚拟路径 → 真实路径：原样返回。"""
        return virtual_path

    def translate_command(self, command: str) -> str:
        """命令翻译：原样返回。"""
        return command

    def mask_output(self, output: str) -> str:
        """输出脱敏 / 回写：原样返回。"""
        return output