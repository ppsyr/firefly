"""DockerPathTranslator — Docker 路径翻译器（容器内直传 + 反向映射到 host）。

【整体职责】
实现 path_translator 契约的"Docker 版本"：正向翻译（translate_path /
translate_command / mask_output）全部直传——容器内 /mnt/poirot/user-data
就是 bind mount 的物理路径，虚拟路径与容器路径本就一致，无需翻译。
额外提供反向映射 reverse_translate，把容器内虚拟路径还原为宿主机物理路径，
供 artifact 提取（如 Sandbox.get_host_path）使用。

【内容摘要】
- _VIRTUAL_PREFIX  : 唯一可反向映射的虚拟路径前缀（/mnt/poirot/user-data）。
- DockerPathTranslator : Docker 路径翻译器类。
    ├─ __init__            : 由 sandbox_root + sandbox_id 推出 host 根路径。
    ├─ translate_path      : 虚拟 → 容器路径，直传（原样返回）。
    ├─ translate_command   : 命令翻译，直传（原样返回）。
    ├─ mask_output         : 输出脱敏，直传（原样返回）。
    └─ reverse_translate   : 虚拟 → host 物理路径（供 artifact 提取）。

【职责边界】
- 只负责：正向直传（容器内路径本就对齐）、反向映射（虚拟 → host 物理路径）。
- 不负责：安全校验（guard）、命令执行（backend）、容器内路径的校验规则
  （由 DockerPathGuard 负责）。
- 持有 host_root：由构造参数 sandbox_root + sandbox_id 拼出，构造后不可变。

【INVARIANT】
- translate_path / translate_command / mask_output 均为直传（与 IdentityTranslator 同）；
  容器内 /mnt/poirot/user-data = bind mount 物理路径，无需转换。
- reverse_translate 仅接受 _VIRTUAL_PREFIX 前缀：
    - virtual_path == _VIRTUAL_PREFIX（精确匹配），或
    - virtual_path 以 _VIRTUAL_PREFIX + "/" 开头；
  其余一律抛 ValueError。
- reverse_translate 输出统一正斜杠（shutil.copy2 在 Windows 上兼容两种分隔符）。
- host_root 由 sandbox_root + sandbox_id 构造时确定，不可变。
- 正向与反向不对称：正向直传（虚拟 == 容器路径），反向才做映射（容器 → host）。
"""
from __future__ import annotations

from pathlib import Path

_VIRTUAL_PREFIX = "/mnt/poirot/user-data"


class DockerPathTranslator:
    """Docker 路径翻译器：容器内直传 + 反向映射到 host。

    - translate_path / translate_command / mask_output：直传
      （容器内 /mnt/poirot/user-data = bind mount 物理路径）。
    - reverse_translate：反向映射，供 Sandbox.get_host_path 用（artifact 提取）。
    """

    def __init__(self, sandbox_root: str | Path, sandbox_id: str) -> None:
        self._host_root = str(Path(sandbox_root) / sandbox_id).replace("\\", "/")

    def translate_path(self, virtual_path: str) -> str:
        """虚拟 → 容器路径：直传（容器内路径本就对齐）。"""
        return virtual_path

    def translate_command(self, command: str) -> str:
        """命令翻译：直传。"""
        return command

    def mask_output(self, output: str) -> str:
        """输出脱敏：直传（容器路径不构成泄漏）。"""
        return output

    def reverse_translate(self, virtual_path: str) -> str:
        """虚拟路径 → host 物理路径（供 artifact 提取）。

        /mnt/poirot/user-data/foo → <sandbox_root>/<sandbox_id>/foo。
        非 /mnt/poirot/user-data 前缀（精确匹配或带 /）抛 ValueError。
        输出统一正斜杠（shutil.copy2 在 Windows 上兼容两种分隔符）。
        """
        if virtual_path != _VIRTUAL_PREFIX and not virtual_path.startswith(
            _VIRTUAL_PREFIX + "/"
        ):
            raise ValueError(f"path not under {_VIRTUAL_PREFIX}: {virtual_path}")
        relative = virtual_path[len(_VIRTUAL_PREFIX):].lstrip("/")
        return f"{self._host_root}/{relative}" if relative else self._host_root