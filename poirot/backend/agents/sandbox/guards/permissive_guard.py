"""PermissiveGuard — 宽松守卫，全放行（Docker / E2B 场景）。

【整体职责】
实现 security_guard 契约的"零拦截"版本：validate_path / validate_command 均为 no-op。
用于容器已提供硬隔离的场景——容器边界（namespace + cgroup）本身就是安全边界，
路径与命令的约束由容器负责，Guard 层不再重复检查。

【内容摘要】
- PermissiveGuard      : 宽松守卫类，所有校验方法 no-op。
    ├─ validate_path    : 路径校验，直接放行。
    └─ validate_command : 命令校验，直接放行。

【职责边界】
- 只负责：满足 security_guard 契约（提供两个 no-op 方法）。
- 不负责：任何实际校验逻辑（由 LocalSecurityGuard / AuditGuard 等负责）、
  容器隔离本身的实现（由 Docker 运行时负责）。
- 无状态：不持有任何字段，所有方法为空实现。

【INVARIANT】
- 所有校验方法 no-op：不抛异常、不返回拒绝、不记录。
- 安全性依赖外部边界：仅在容器已提供硬隔离时使用；
  在本地（无隔离）场景使用会失去所有命令/路径约束。
- 通常作为 AuditGuard 的 inner 使用：Docker → AuditGuard(PermissiveGuard)，
  由 AuditGuard 在外层补"分级 + 审计"。
"""
from __future__ import annotations


class PermissiveGuard:
    """宽松守卫：容器已隔离，全放行。

    容器边界（namespace + cgroup）即安全边界，
    路径白名单由容器负责，Guard 层 no-op。
    """

    def validate_path(self, path: str, *, write: bool = False) -> None:
        pass

    def validate_command(self, command: str) -> None:
        pass