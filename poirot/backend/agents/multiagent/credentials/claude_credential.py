"""ClaudeCredentialProvider — 读 ~/.claude/.credentials.json 复用 Claude Code CLI 登录态。

【整体职责】
发现 Claude Code CLI 的 OAuth 凭证：优先读环境变量直接提供的 token，
否则读 credentials 文件（路径可被 env 覆盖，默认 ~/.claude/.credentials.json），
解析出 accessToken / refreshToken / expiresAt，返回 ClaudeCredential。
凭证缺失或过期时返回 None（不抛异常）。

【内容摘要】
- ClaudeCredential              : 凭证数据类，含 access_token / refresh_token / expires_at。
- ClaudeCredentialProvider      : 凭证发现主类，实现 get_credential()。
- get_credential()              : 按三级查找顺序发现凭证，返 ClaudeCredential | None。
- _resolve_path()               : 解析 credentials 文件路径（env 覆盖 + 默认路径）。
- _load_json()                  : 读取并解析 JSON 文件，失败返回 None。

【职责边界】
- 只负责：发现凭证、读取文件、解析字段、过期检测。
- 不负责：刷新凭证、存储凭证、管理凭证生命周期、写 ThreadState。
- 不持有运行时状态：每次 get_credential 重新读取。

【INVARIANT】
- 凭证不写 ThreadState：返回的 ClaudeCredential 只传给 specialist runtime。
- 三级查找顺序固定：
  1. $CLAUDE_CODE_OAUTH_TOKEN 或 $ANTHROPIC_AUTH_TOKEN（直接 token）
  2. $CLAUDE_CODE_CREDENTIALS_PATH（指定文件路径）
  3. ~/.claude/.credentials.json（默认路径）
- 凭证缺失或过期返 None，不抛异常（调用方据此把 specialist 标 disabled）。
- 过期检测：expires_at 为毫秒时间戳，留 1 分钟 buffer；expires_at <= 0 视为无过期信息，不检测。
- 所有 IO 失败（文件不存在 / 是目录 / JSON 解析失败 / 读取异常）统一返 None。
- kind 固定为 "claude"。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from poirot.backend.agents.multiagent.credential_provider import Credential


@dataclass(frozen=True)
class ClaudeCredential(Credential):
    """Claude Code CLI OAuth 凭证。

    kind 固定 "claude"（继承 Credential 基类）。
    expires_at 是毫秒时间戳；0 表示无过期信息，不做过期检测。
    """

    access_token: str
    refresh_token: str = ""
    expires_at: int = 0

    @property
    def is_expired(self) -> bool:
        """过期检测：expires_at 为毫秒时间戳，留 1 分钟 buffer。

        expires_at <= 0 时视为无过期信息，返回 False。
        """
        if self.expires_at <= 0:
            return False
        return time.time() * 1000 > self.expires_at - 60_000


class ClaudeCredentialProvider:
    """读 ~/.claude/.credentials.json 的凭证发现器，支持 env 覆盖。

    查找顺序：
    1. $CLAUDE_CODE_OAUTH_TOKEN / $ANTHROPIC_AUTH_TOKEN（直接 token）
    2. $CLAUDE_CODE_CREDENTIALS_PATH（指定 credentials 文件）
    3. ~/.claude/.credentials.json（默认路径）

    凭证缺失或过期返回 None，不抛异常。
    """

    def get_credential(self) -> ClaudeCredential | None:
        """发现 Claude 凭证。

        优先读环境变量直接提供的 token；否则读 credentials 文件并解析。
        缺失 / 过期 / 解析失败均返回 None。
        """
        direct_token = (
            os.getenv("CLAUDE_CODE_OAUTH_TOKEN")
            or os.getenv("ANTHROPIC_AUTH_TOKEN")
        )
        if direct_token and direct_token.strip():
            return ClaudeCredential(
                kind="claude",
                access_token=direct_token.strip(),
            )

        cred_path = self._resolve_path()
        data = self._load_json(cred_path)
        if data is None:
            return None

        oauth = data.get("claudeAiOauth", {})
        if not isinstance(oauth, dict):
            oauth = {}
        access_token = oauth.get("accessToken", "")
        if not access_token:
            return None

        cred = ClaudeCredential(
            kind="claude",
            access_token=access_token,
            refresh_token=oauth.get("refreshToken", ""),
            expires_at=oauth.get("expiresAt", 0),
        )

        if cred.is_expired:
            return None

        return cred

    def _resolve_path(self) -> Path:
        """解析 credentials 文件路径：env 覆盖优先，否则 ~/.claude/.credentials.json。"""
        configured = os.getenv("CLAUDE_CODE_CREDENTIALS_PATH")
        if configured:
            return Path(configured).expanduser()
        return Path.home() / ".claude" / ".credentials.json"

    def _load_json(self, path: Path) -> dict | None:
        """读取并解析 JSON 文件。不存在 / 是目录 / 解析失败 / IO 异常均返回 None。"""
        if not path.exists() or path.is_dir():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None