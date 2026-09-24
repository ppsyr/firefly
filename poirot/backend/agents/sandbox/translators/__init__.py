"""Sandbox PathTranslator 实现层 — 路径翻译器实现的统一出口。

【整体职责】
汇总 sandbox 层的 path translator 实现，对外统一暴露入口。调用方（provider / 装配层）
从本包导入 translator，无需关心具体实现来自哪个文件。各实现均满足
contracts/path_translator.py 定义的契约。

【内容摘要】
- IdentityTranslator    : 恒等翻译器——三个契约方法全部直传，不做任何翻译；
                          用于虚拟路径与真实路径本就一致的场景。
- LocalPathTranslator   : 本地翻译器——基于 PathMapping 做虚拟 ↔ 物理双向翻译
                          与输出脱敏；用于本地沙箱。
- DockerPathTranslator  : Docker 翻译器——正向三个契约方法直传（容器内路径与虚拟
                          路径对齐），额外提供反向映射供 artifact 提取；用于 Docker 沙箱。

【职责边界】
- 只负责：汇总并导出 path translator 实现类。
- 不负责：具体翻译逻辑（由各实现文件负责）、契约定义
  （contracts/path_translator.py）、translator 的选择与装配（由 provider / 配置决定）。

【INVARIANT】
- 所有导出类均满足 contracts/path_translator.py 契约
  （translate_path / translate_command / mask_output）。
- 三者为"三选一"关系，由场景决定用哪个：路径不对齐用 Local，对齐且无需反向用
  Identity，对齐且需反向映射（artifact 提取）用 Docker。
- DockerPathTranslator 额外提供 reverse_translate（非契约方法），仅供 Docker 场景
  的 artifact 提取使用。
- 本文件只做 re-export，不含任何逻辑。
"""
from poirot.backend.agents.sandbox.translators.identity_translator import (
    IdentityTranslator,
)
from poirot.backend.agents.sandbox.translators.local_path_translator import (
    LocalPathTranslator,
)
from poirot.backend.agents.sandbox.translators.docker_path_translator import (
    DockerPathTranslator,
)

__all__ = ["IdentityTranslator", "LocalPathTranslator", "DockerPathTranslator"]