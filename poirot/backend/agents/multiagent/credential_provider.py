"""CredentialProvider Protocol — specialist 凭证发现契约。

【整体职责】
定义"如何发现 specialist 凭证"的契约：只发现、不管理。凭证由 CLI 登录态提供，
Poirot 读取后仅传给 specialist runtime，不写入任何状态、不做刷新或存储。

【内容摘要】
- Credential(基类, frozen)         : 凭证数据结构，含 kind 标识 specialist 类型。
- CredentialProvider(Protocol)     : 凭证发现契约，仅一个 get_credential() 方法。

【职责边界】
- 只负责：发现凭证（读 CLI 登录态文件）。
- 不负责：刷新凭证、存储凭证、管理凭证生命周期、写 ThreadState。
- 不持有状态：Protocol 无实现，纯接口。

【INVARIANT】
- 凭证不进 LLM 主态：CredentialProvider 返回的 token 只传给 specialist runtime，
  不写 ThreadState（避免凭证泄漏进 LLM 上下文）。
- 复用 CLI 登录态：Codex 读 ~/.codex/auth.json，Claude 读 ~/.claude/.credentials.json。
- Poirot 不管理凭证：只发现，不刷新、不存储。
- 凭证缺失返 None：调用方据此把 specialist 标 disabled，不注册对应 tool。
- 实现示例：CodexCredentialProvider / ClaudeCredentialProvider。
- Credential.kind 标识 specialist 类型（"codex" / "claude"）。
"""
from __future__ import annotations

from dataclasses import dataclass

from typing import Protocol


@dataclass(frozen=True)
class Credential:
    """specialist 凭证基类。

    凭证只传给 specialist runtime，不写 ThreadState（避免进入 LLM 上下文）。
    kind 标识 specialist 类型（"codex" / "claude"）。
    """

    kind: str


class CredentialProvider(Protocol):
    """specialist 凭证发现契约（复用 CLI 登录态，不管理凭证）。

    实现示例：CodexCredentialProvider / ClaudeCredentialProvider。
    """

    def get_credential(self) -> Credential | None:
        """发现 specialist 凭证。

        返回 None 表示凭证缺失：调用方据此把 specialist 标 disabled，不注册对应 tool。
        返回的 Credential 只传给 specialist runtime，不写 ThreadState。
        """
        ...