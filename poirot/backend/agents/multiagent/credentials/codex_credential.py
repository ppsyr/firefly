"""CodexCredentialProvider — 读 ~/.codex/auth.json 复用 Codex CLI 登录态。

【整体职责】
发现 Codex CLI 的凭证：读 auth.json（路径可被 $CODEX_AUTH_PATH 覆盖，
默认 ~/.codex/auth.json），兼容 legacy 与 nested 两种 JSON 格式，
解析出 access_token / account_id，返回 CodexCredential。
凭证缺失时返回 None（不抛异常）。

【内容摘要】
- CodexCredential              : 凭证数据类，含 access_token / account_id。
- CodexCredentialProvider      : 凭证发现主类，实现 get_credential()。
- get_credential()             : 读文件 → 兼容两种格式 → 解析字段，返 CodexCredential | None。
- _resolve_path()              : 解析 auth.json 路径（env 覆盖 + 默认路径）。
- _load_json()                 : 读取并解析 JSON 文件，失败返回 None。

【职责边界】
- 只负责：发现凭证、读取文件、兼容两种 JSON 格式、解析字段。
- 不负责：刷新凭证、存储凭证、管理凭证生命周期、写 ThreadState。
- 不持有运行时状态：每次 get_credential 重新读取。

【INVARIANT】
- 凭证不写 ThreadState：返回的 CodexCredential 只传给 specialist runtime。
- 两种 JSON 格式兼容：
  - legacy：{"access_token": "...", "account_id": "..."}（top-level）
  - nested：{"tokens": {"access_token": "...", "account_id": "..."}}
- access_token 查找顺序固定：
  1. data["access_token"]（legacy）
  2. data["token"]（另一种 legacy 写法）
  3. data["tokens"]["access_token"]（nested）
- account_id 查找顺序：data["account_id"] > data["tokens"]["account_id"]。
- 无 access_token → 返回 None（调用方据此把 specialist 标 disabled）。
- 所有 IO 失败（文件不存在 / 是目录 / JSON 解析失败 / 读取异常）统一返 None。
- kind 固定为 "codex"。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from poirot.backend.agents.multiagent.credential_provider import Credential


@dataclass(frozen=True)
class CodexCredential(Credential):
    """Codex CLI 凭证（access_token + account_id）。

    kind 固定 "codex"（继承 Credential 基类）。
    """

    access_token: str
    account_id: str = ""


class CodexCredentialProvider:
    """读 ~/.codex/auth.json 的凭证发现器，支持 $CODEX_AUTH_PATH 覆盖。

    兼容 legacy（top-level）与 nested（tokens.*）两种 JSON 格式。
    凭证缺失返回 None，不抛异常。
    """

    def get_credential(self) -> CodexCredential | None:
        """发现 Codex 凭证。

        流程：_resolve_path → _load_json → 兼容两种格式解析字段。
        无 access_token 或解析失败均返回 None。
        """
        cred_path = self._resolve_path()
        data = self._load_json(cred_path)
        if data is None:
            return None

        tokens = data.get("tokens", {})
        if not isinstance(tokens, dict):
            tokens = {}

        access_token = (
            data.get("access_token")
            or data.get("token")
            or tokens.get("access_token", "")
        )
        account_id = data.get("account_id") or tokens.get("account_id", "")

        if not access_token:
            return None

        return CodexCredential(
            kind="codex",
            access_token=access_token,
            account_id=account_id,
        )

    def _resolve_path(self) -> Path:
        """解析 auth.json 路径：$CODEX_AUTH_PATH 优先，否则 ~/.codex/auth.json。"""
        configured = os.getenv("CODEX_AUTH_PATH")
        if configured:
            return Path(configured).expanduser()
        return Path.home() / ".codex" / "auth.json"

    def _load_json(self, path: Path) -> dict | None:
        """读取并解析 JSON 文件。不存在 / 是目录 / 解析失败 / IO 异常均返回 None。"""
        if not path.exists() or path.is_dir():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None