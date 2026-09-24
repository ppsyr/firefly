"""Sandbox Local Provider 实现层 — 本地 provider 的统一出口。

【整体职责】
汇总 sandbox 层的本地 provider 实现，对外统一暴露入口。调用方（装配层 / 上层）
从本包导入本地 provider，无需关心具体实现来自哪个文件。

【内容摘要】
- local_sandbox_provider : LocalSandboxProvider —— 本地沙箱 provider
                           （LRU 缓存 + 确定性 ID + 组合 Local 三件套）。

【职责边界】
- 只负责：汇总并导出本地 provider 实现。
- 不负责：具体实现逻辑（由 local_sandbox_provider.py 负责）、
  provider 契约定义（contracts/sandbox_provider.py）、
  runtime / translator / guard 的实现（各自在 runtimes / translators / guards 下）。

【INVARIANT】
- 本地 provider 组合：LocalRuntime + LocalPathTranslator + AuditGuard(LocalSecurityGuard)。
- 本地无容器基础设施层（无 backend / executor）——这是与 Docker 实现的核心差异。
- 本文件只做 re-export，不含任何逻辑。
"""
from poirot.backend.agents.sandbox.local.local_sandbox_provider import (
    LocalSandboxProvider,
)

__all__ = ["LocalSandboxProvider"]