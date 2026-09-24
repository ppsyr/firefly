"""sandbox_id 格式校验工具 — 防不可信 sandbox_id 引发路径穿越（S6 defense-in-depth）。

【整体职责】
校验 sandbox_id 是否符合约定格式（8 位小写十六进制，来自 sha256[:8]）。
用于把不可信来源的 sandbox_id 挡在"路径拼接"之前——
避免 Path(root) / sandbox_id 中若含 "../" 造成 path traversal。

【内容摘要】
- _SANDBOX_ID_RE        : sandbox_id 格式正则 ^[a-f0-9]{8}$。
- validate_sandbox_id   : 校验入口；不合法抛 ValueError。

【职责边界】
- 只负责：格式校验（正则匹配 + 类型检查）。
- 不负责：sandbox_id 的生成（由 _deterministic_sandbox_id 负责）、
  路径拼接本身（由调用方负责）。
- 无状态：不持有任何字段，纯函数式校验。

【为什么需要（威胁模型）】
- 正常路径：sandbox_id 来自 _deterministic_sandbox_id（sha256[:8]），当前安全。
- 风险路径：反序列化（SandboxInfo.from_dict 从文件恢复）可能引入不可信值。
  若该值被直接用于 Path(root) / sandbox_id，且含 "../"，即可造成路径穿越。
- 本模块是 defense-in-depth：即使上游信任链被破坏，也能在路径拼接前挡住。

【INVARIANT】
- 合法格式：恰好 8 位，字符集 [a-f0-9]（小写十六进制，sha256[:8]）。
- 类型检查：非 str 也判为非法（防 None / 数字等混入）。
- 校验失败统一抛 ValueError（不做静默修复、不做截断、不做大小写转换）。
"""
from __future__ import annotations

import re

_SANDBOX_ID_RE = re.compile(r"^[a-f0-9]{8}$")


def validate_sandbox_id(sandbox_id: str) -> None:
    """校验 sandbox_id 格式：必须是 8 位小写十六进制（sha256[:8]）。

    Raises:
        ValueError: sandbox_id 不是 str，或不匹配 ^[a-f0-9]{8}$。
    """
    if not isinstance(sandbox_id, str) or not _SANDBOX_ID_RE.match(sandbox_id):
        raise ValueError(f"invalid sandbox_id: {sandbox_id!r}")