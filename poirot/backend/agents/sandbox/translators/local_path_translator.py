"""LocalPathTranslator — 本地路径翻译器（PathMapping 双向翻译 + 输出脱敏）。

【整体职责】
实现 path_translator 契约的"本地版本"：基于 PathMapping 列表，
在虚拟路径 ↔ 物理路径之间做双向翻译，并对命令、输出做相应替换。
- 正向：虚拟路径 → 物理路径（translate_path / translate_command）
- 反向：物理路径 → 虚拟路径（mask_output，输出脱敏）
用于本地沙箱——虚拟路径与宿主真实路径不一致、必须显式映射的场景。
Docker / E2B 场景改用 IdentityTranslator（路径本就对齐，直传即可）。

【内容摘要】
- LocalPathTranslator                     : 本地路径翻译器类。
    ├─ __init__                              : 持有 PathMapping 列表。
    ├─ _mappings_by_container_specificity    : 按 container_path 长度降序（前向解析用）。
    ├─ _mappings_by_local_specificity        : 按 local_path 长度降序（反向解析用）。
    ├─ _resolved_local_paths                 : 每个 mapping 的 resolve() 后物理路径（缓存 syscall）。
    ├─ _command_pattern                      : 命令中容器路径的正则（shell-aware 边界）。
    ├─ _reverse_output_patterns              : 物理→虚拟的反向正则列表（输出脱敏用）。
    ├─ _resolve_path_with_mapping            : 虚拟路径 → ResolvedPath（物理路径 + 命中 mapping）。
    ├─ translate_path                        : 虚拟路径 → 物理路径。
    ├─ translate_command                     : 命令中的容器路径 → 物理路径。
    └─ mask_output                           : 物理路径 → 虚拟路径（脱敏）。

【职责边界】
- 只负责：虚拟 ↔ 物理路径的双向翻译、命令中的路径替换、输出的反向脱敏。
- 不负责：安全校验（guard）、命令执行（backend）、路径匹配规则的定义
  （由装配层注入 PathMapping）。
- 持有 PathMapping 列表：映射规则来源，构造时注入；翻译规则全由 mapping 决定。

【INVARIANT】
- translate_path 幂等：同一虚拟路径多次翻译结果一致。
- mask_output 是 translate_path 的逆操作：物理路径 → 虚拟路径。
- 匹配规则：按 container_path 长度降序（前向）、local_path 长度降序（反向）——
  最长前缀优先，防 /mnt/poirot/skills 误匹配 /mnt/poirot/skills-extra。
- 路径穿越拒绝：resolve() 跟随 symlink 后做越界检测，防止 symlink 逃逸。
- 缓存：四个 cached_property 在首次访问后复用，避免重复排序 / syscall / 正则编译。
- 无命中 mapping 时：translate_path 原样返回虚拟路径（ResolvedPath.mapping = None）。
"""
from __future__ import annotations

import re
from functools import cached_property
from pathlib import Path

from poirot.backend.agents.sandbox.types import PathMapping, ResolvedPath


class LocalPathTranslator:
    """本地路径翻译器：基于 PathMapping 做虚拟 ↔ 物理双向翻译 + 输出脱敏。

    Local 场景使用 PathMapping 翻译虚拟 → 物理；
    Docker / E2B 场景改用 IdentityTranslator 直传。

    核心约束见模块级 INVARIANT。
    """

    def __init__(self, path_mappings: list[PathMapping]) -> None:
        self._mappings = path_mappings

    @cached_property
    def _mappings_by_container_specificity(self) -> list[PathMapping]:
        """按 container_path 长度降序排序（前向解析用，最长前缀优先）。"""
        return sorted(self._mappings, key=lambda m: len(m.container_path), reverse=True)

    @cached_property
    def _mappings_by_local_specificity(self) -> list[PathMapping]:
        """按 local_path 长度降序排序（反向解析用，最长前缀优先）。"""
        return sorted(self._mappings, key=lambda m: len(m.local_path), reverse=True)

    @cached_property
    def _resolved_local_paths(self) -> dict[PathMapping, str]:
        """每个 mapping 的 local_path 经 resolve() 后的物理路径（缓存，避免重复 syscall）。"""
        return {m: str(Path(m.local_path).resolve()) for m in self._mappings}

    @cached_property
    def _command_pattern(self) -> re.Pattern[str] | None:
        """bash 命令中容器路径的匹配器（shell-aware 边界）。

        边界字符集：/ | $ | 空白 | 引号 | ; & | < > ( )，避免误匹配子串。
        """
        patterns = [
            re.escape(m.container_path) + r"(?=/|$|[\s\"';&|<>()])"
            for m in self._mappings_by_container_specificity
        ]
        return re.compile("|".join(f"({p})" for p in patterns)) if patterns else None

    @cached_property
    def _reverse_output_patterns(self) -> list[tuple[re.Pattern[str], PathMapping, str]]:
        """物理路径 → 虚拟路径的反向匹配（输出脱敏用），按 local_path 长度降序。

        返回 (pattern, mapping, local) 三元组：pattern 匹配 local + 子路径。
        """
        result: list[tuple[re.Pattern[str], PathMapping, str]] = []
        for m in self._mappings_by_local_specificity:
            local = self._resolved_local_paths[m]
            escaped = re.escape(local)
            path_re = re.compile(escaped + r"(?:[\\/][^\\/\s\"';&|<>()]*)*")
            result.append((path_re, m, local))
        return result

    def _resolve_path_with_mapping(self, virtual_path: str) -> ResolvedPath:
        """虚拟路径 → ResolvedPath（含物理路径 + 命中的 mapping）。

        按 container_path 最长前缀匹配；命中后 resolve 并做越界检测；
        未命中任何 mapping 时原样返回虚拟路径（mapping=None）。
        """
        for mapping in self._mappings_by_container_specificity:
            container_path = mapping.container_path.rstrip("/")
            if virtual_path == container_path or virtual_path.startswith(
                container_path + "/"
            ):
                relative = virtual_path[len(container_path):].lstrip("/")
                local_root = Path(self._resolved_local_paths[mapping])
                resolved = (local_root / relative).resolve()
                try:
                    resolved.relative_to(local_root)
                except ValueError as exc:
                    raise PermissionError(
                        f"path traversal detected: {virtual_path}"
                    ) from exc
                return ResolvedPath(str(resolved), mapping)
        return ResolvedPath(virtual_path, None)

    def translate_path(self, virtual_path: str) -> str:
        """虚拟路径 → 物理路径；无命中 mapping 时原样返回。"""
        return self._resolve_path_with_mapping(virtual_path).path

    def translate_command(self, command: str) -> str:
        """把命令中的容器路径替换为物理路径；无 mapping 时原样返回。"""
        if self._command_pattern is None:
            return command
        return self._command_pattern.sub(
            lambda m: self.translate_path(m.group(0)), command
        )

    def mask_output(self, output: str) -> str:
        """物理路径 → 虚拟路径（输出脱敏）。

        匹配 local_root + 子路径，替换为 container + 子路径（\\ 转 /）。
        """
        result = output
        for path_re, mapping, local in self._reverse_output_patterns:
            container = mapping.container_path

            def replace_match(match: re.Match[str], container=container, local=local) -> str:
                matched = match.group(0)
                relative = matched[len(local):].replace("\\", "/")
                return f"{container}{relative}"

            result = path_re.sub(replace_match, result)
        return result