"""Prompt 管理系统 — 统一加载 / 渲染 / 切换 prompt。

【整体职责】
提供提示词的统一加载、变量渲染与切换能力，是唯一直接访问 prompt 文件（.md）的层。
按三层架构组织：本模块为 low-level（唯一碰文件），模块 accessor 为 mid-level
（知道 category/name + vars），外部调用方为 high-level（只调 accessor）。

【内容摘要】
- PromptManager          : 统一 prompt 加载 / 渲染 / 切换管理（唯一碰文件的层）。
- PromptManager.load     : 加载并渲染 prompt（user/ 优先 system/）。
- PromptManager.load_raw : 加载原始文本（不渲染），返回 (text, source)。
- PromptManager.list_prompts : 列出可用 prompt（category/name 格式）。
- PromptManager.clear_cache  : 清空缓存。
- get_prompt_manager     : 全局单例访问器。

【职责边界】
- 只负责：读 .md 文件、缓存、变量渲染、列出与切换（user/ 覆盖 system/）。
- 不负责：知道具体 category/name 与变量语义（模块 accessor 层）、业务级 prompt 组合
  （外部调用方）、prompt 内容的编写（.md 文件本身）。

【INVARIANT】
- 三层架构：本模块 low-level（唯一碰文件）→ accessor mid-level → 调用方 high-level。
- 持久化形态：prompt 存为 .md 文件。
- 渲染语法：${variable} regex 替换；未绑定的变量保留原样并打 warning 到 stderr。
- 覆盖规则：user/ 优先于 system/。
- 缓存：按文件路径缓存读取结果，clear_cache 手动失效。
- 单例：get_prompt_manager 全局唯一实例，base_dir 固定为本文件所在目录。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any


class PromptManager:
    """统一 prompt 加载/渲染/切换管理。

    Attributes:
        _base: prompt 根目录，下含 system/ 与 user/ 两层。
        _cache: 文件路径 → 文本内容的缓存。
    """

    def __init__(self, base_dir: Path) -> None:
        """初始化。

        Args:
            base_dir: prompt 根目录（含 system/ 与 user/ 子目录）。
        """
        self._base = base_dir
        self._cache: dict[str, str] = {}

    def load(self, category: str, name: str, **vars: Any) -> str:
        """加载 prompt + ${var} 渲染。user/ 优先 system/。

        Args:
            category: prompt 分类（对应子目录名）。
            name: prompt 名（对应 .md 文件名，不含扩展名）。
            **vars: 渲染变量，替换模板中的 ${key}。

        Returns:
            str: 渲染后的 prompt 文本。

        Raises:
            FileNotFoundError: prompt 文件不存在时。
        """
        template, _source = self._read(category, name)
        return self._render(template, vars)

    def load_raw(self, category: str, name: str) -> tuple[str, str]:
        """加载原始文本（不渲染）。返回 (text, source)。source="user"|"system"。

        Args:
            category: prompt 分类。
            name: prompt 名。

        Returns:
            tuple[str, str]: (原始文本, 来源层)。

        Raises:
            FileNotFoundError: prompt 文件不存在时。
        """
        return self._read(category, name)

    def list_prompts(self, category: str | None = None) -> list[str]:
        """列出可用 prompt（category/name 格式）。

        Args:
            category: 可选，仅列出该分类；为 None 时列出全部。

        Returns:
            list[str]: 排序后的 "category/name" 列表（合并 system/ 与 user/）。
        """
        result: set[str] = set()
        for layer in ("system", "user"):
            base = self._base / layer
            if not base.exists():
                continue
            for cat_dir in base.iterdir():
                if not cat_dir.is_dir():
                    continue
                if category and cat_dir.name != category:
                    continue
                for md in cat_dir.glob("*.md"):
                    result.add(f"{cat_dir.name}/{md.stem}")
        return sorted(result)

    def clear_cache(self) -> None:
        """清空读取缓存。"""
        self._cache.clear()

    def _read(self, category: str, name: str) -> tuple[str, str]:
        """读 .md 文件。user/ 优先。返回 (text, source)。

        Args:
            category: prompt 分类。
            name: prompt 名。

        Returns:
            tuple[str, str]: (文本, 来源层 "user" | "system")。

        Raises:
            FileNotFoundError: user/ 与 system/ 下均不存在时。
        """
        user_path = self._base / "user" / category / f"{name}.md"
        sys_path = self._base / "system" / category / f"{name}.md"
        if user_path.exists():
            return self._cached_read(user_path), "user"
        if sys_path.exists():
            return self._cached_read(sys_path), "system"
        raise FileNotFoundError(f"prompt not found: {category}/{name}")

    def _cached_read(self, path: Path) -> str:
        """按路径缓存读取文件内容。

        Args:
            path: 文件路径。

        Returns:
            str: 文件文本内容（UTF-8）。
        """
        key = str(path)
        if key not in self._cache:
            self._cache[key] = path.read_text(encoding="utf-8")
        return self._cache[key]

    @staticmethod
    def _render(template: str, vars: dict[str, Any]) -> str:
        """${variable} regex 替换。未匹配保留原样 + warning。

        Args:
            template: 模板文本。
            vars: 变量字典。

        Returns:
            str: 替换后的文本；未绑定的占位符保留原样。
        """
        def replacer(m: re.Match) -> str:
            key = m.group(1)
            if key in vars:
                return str(vars[key])
            print(f"[PromptManager] warning: unbound ${{{key}}}", file=sys.stderr)
            return m.group(0)
        return re.sub(r"\$\{(\w+)\}", replacer, template)


_pm: PromptManager | None = None


def get_prompt_manager() -> PromptManager:
    """获取全局 PromptManager 单例（base_dir 为本文件所在目录）。"""
    global _pm
    if _pm is None:
        _pm = PromptManager(Path(__file__).parent)
    return _pm