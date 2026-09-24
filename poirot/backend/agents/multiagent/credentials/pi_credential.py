"""PiCredentialProvider — 双轨凭证解析（config 优先 + env 兜底）。

【整体职责】
发现 Pi specialist 的凭证：先确认 pi CLI 可执行，再按四级优先级解析凭证
（config 显式 api_key > config 显式 provider 对应的 env > 遍历所有 provider env >
auth.json 文件），返回 PiCredential。凭证缺失时返回 None（不抛异常）。

【内容摘要】
- PiCredential               : 凭证数据类，含 provider / api_key / auth_file。
- PiCredentialProvider       : 凭证发现主类，实现 get_credential()。
- get_credential()           : 四级优先级解析凭证，返 PiCredential | None。
- _is_pi_installed()         : 检测 pi CLI 是否在 PATH。
- _resolve_auth_path()       : 解析 auth.json 路径（支持 PI_CODING_AGENT_DIR 覆盖）。
- _provider_env_map()        : provider → env var 映射（国内 provider 靠前）。

【职责边界】
- 只负责：检测 pi CLI、按优先级解析凭证、读取文件路径。
- 不负责：刷新凭证、存储凭证、管理凭证生命周期、写 ThreadState。
- 不持有运行时状态：每次 get_credential 重新解析。

【INVARIANT】
- 凭证不写 ThreadState：返回的 PiCredential 只传给 specialist runtime。
- 前置条件：pi CLI 必须在 PATH；不在则直接返回 None（即使有 API key）。
- 四级解析优先级固定：
  1. config 显式 api_key（config_provider 缺省时默认 "anthropic"）
  2. config 显式 provider → 对应 env var
  3. 遍历 _provider_env_map（国内 provider 靠前）
  4. auth.json 文件（~/.pi/agent/auth.json，支持 PI_CODING_AGENT_DIR 覆盖）
- 不强制要求凭证文件存在：任意 API key env 都能用。
- 凭证缺失返 None，不抛异常（调用方据此把 specialist 标 disabled）。
- 国内 provider 优先（DeepSeek / Kimi / MiniMax / Xiaomi / ZAI 靠前）——
  顺序即优先级，便宜优先。
- kind 固定为 "pi"。
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from poirot.backend.agents.multiagent.credential_provider import Credential


@dataclass(frozen=True)
class PiCredential(Credential):
    """Pi agent 凭证（kind 固定 "pi"）。

    三个字段按来源填充：
    - provider: 用户偏好的 provider（anthropic / deepseek / kimi / ...）
    - api_key: 直接 API key（config 显式或 env 命中时填）
    - auth_file: auth.json 路径（env 兜底时填）
    """

    kind: str = "pi"
    provider: str | None = None
    api_key: str | None = None
    auth_file: str | None = None


class PiCredentialProvider:
    """双轨凭证解析：config 优先 + env 兜底。

    与 CodexCredentialProvider（读 ~/.codex/auth.json）不同：
    - Pi 不强制要求凭证文件存在；
    - 任何 API key env var 都能用；
    - 只需 pi CLI 可执行（PATH 查找）+ 任意一个 API key 即可。

    四级解析优先级：
    1. config 显式 api_key（最高）
    2. config 显式 provider → 对应 env var
    3. 遍历所有 provider env var（国内 provider 优先）
    4. auth.json 文件（~/.pi/agent/auth.json）
    """

    def __init__(
        self,
        config_provider: str | None = None,
        config_api_key: str | None = None,
    ) -> None:
        """初始化。

        Args:
            config_provider: config 里显式指定的 provider（可选）。
            config_api_key: config 里显式指定的 API key（可选）。
        """
        self._config_provider = config_provider
        self._config_api_key = config_api_key

    def get_credential(self) -> PiCredential | None:
        """发现 Pi 凭证。

        流程：
        1. pi CLI 不在 PATH → None。
        2. config 显式 api_key → 返回（provider 缺省 "anthropic"）。
        3. config 显式 provider → 查对应 env var，命中则返回。
        4. 遍历 _provider_env_map（国内优先）→ 命中则返回。
        5. auth.json 文件存在 → 返回（仅带 auth_file）。
        6. 都未命中 → None。
        """
        # 1. 检测 pi CLI 是否可执行
        if not self._is_pi_installed():
            return None

        # 2. config 显式 api_key（最高优先级）
        if self._config_api_key:
            provider = self._config_provider or "anthropic"  # 默认 anthropic
            return PiCredential(provider=provider, api_key=self._config_api_key)

        # 3. config 显式 provider → 找对应 env var
        if self._config_provider:
            env_var = self._provider_env_map().get(self._config_provider)
            if env_var:
                key = os.getenv(env_var)
                if key:
                    return PiCredential(
                        provider=self._config_provider, api_key=key
                    )

        # 4. 遍历所有 provider env var（国内 provider 优先顺序）
        for provider, env_var in self._provider_env_map().items():
            key = os.getenv(env_var)
            if key:
                return PiCredential(provider=provider, api_key=key)

        # 5. auth.json 文件（~/.pi/agent/auth.json）
        auth_path = self._resolve_auth_path()
        if auth_path and auth_path.exists():
            return PiCredential(auth_file=str(auth_path))

        return None

    def _is_pi_installed(self) -> bool:
        """检测 pi CLI 是否在 PATH。"""
        return shutil.which("pi") is not None

    def _resolve_auth_path(self) -> Path | None:
        """解析 auth.json 路径：PI_CODING_AGENT_DIR 覆盖优先，否则 ~/.pi/agent/auth.json。"""
        configured = os.getenv("PI_CODING_AGENT_DIR")
        if configured:
            return Path(configured) / "auth.json"
        return Path.home() / ".pi" / "agent" / "auth.json"

    def _provider_env_map(self) -> dict[str, str]:
        """Pi 支持的 provider → env var 映射。

        顺序即优先级：国内常用 provider 靠前（便宜优先），国外靠后。
        """
        return {
            # 国内 provider（便宜优先）
            "deepseek": "DEEPSEEK_API_KEY",
            "kimi-coding": "KIMI_API_KEY",
            "minimax": "MINIMAX_API_KEY",
            "xiaomi": "XIAOMI_API_KEY",
            "zai": "ZAI_API_KEY",
            # 国外大厂
            "anthropic": "ANTHROPIC_API_KEY",
            "openai": "OPENAI_API_KEY",
            "google": "GEMINI_API_KEY",
            # 聚合/其他
            "openrouter": "OPENROUTER_API_KEY",
            "groq": "GROQ_API_KEY",
            "xai": "XAI_API_KEY",
            "mistral": "MISTRAL_API_KEY",
            "together": "TOGETHER_API_KEY",
        }