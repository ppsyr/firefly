"""Sandbox SecurityGuard 实现层 — 守卫实现的统一出口。

【整体职责】
汇总 sandbox 层的 guard 实现，对外统一暴露入口。调用方（provider / 装配层）
从本包导入 guard，无需关心具体实现来自哪个文件。各实现均满足
contracts/security_guard.py 定义的契约。

【内容摘要】
- AuditGuard         : 审计守卫——命令三档分级（block / warn / pass）+ 审计日志，
                       组合模式，包装一个底层 guard。
- LocalSecurityGuard : 本地安全守卫——本地场景的命令 / 路径校验规则实现。
- PermissiveGuard    : 宽松守卫——全放行（no-op），用于容器已提供硬隔离的场景。
- DockerPathGuard    : Docker 路径守卫——Docker 场景的路径越界校验实现，
                       约束访问不越出挂载目录。

【职责边界】
- 只负责：汇总并导出 guard 实现类。
- 不负责：具体校验逻辑（由各实现文件负责）、契约定义（contracts/security_guard.py）、
  guard 的选择与装配（由 provider / 配置决定）。

【INVARIANT】
- 所有导出类均满足 contracts/security_guard.py 契约（validate_command / validate_path）。
- AuditGuard 是组合层，不替代底层实现：Local 模式配 LocalSecurityGuard，
  Docker 模式配 PermissiveGuard（分级与审计在 AuditGuard，路径越界由容器兜底）。
- DockerPathGuard 是 Docker 场景的独立路径守卫实现，按需叠加。
- 本文件只做 re-export，不含任何逻辑。
"""
from poirot.backend.agents.sandbox.guards.audit_guard import AuditGuard
from poirot.backend.agents.sandbox.guards.local_security_guard import (
    LocalSecurityGuard,
)
from poirot.backend.agents.sandbox.guards.permissive_guard import (
    PermissiveGuard,
)
from poirot.backend.agents.sandbox.guards.docker_path_guard import (
    DockerPathGuard,
)

__all__ = ["AuditGuard", "LocalSecurityGuard", "PermissiveGuard", "DockerPathGuard"]