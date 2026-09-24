"""本地产物存储 — 把产物写入本地文件系统并返回 Artifact 引用。

【整体职责】
提供产物的本地读写能力：save_artifact 把内容写入 {output_dir}/artifacts/{filename}
并返回 Artifact 记录；get_artifact 按路径读回产物内容。供 CapabilityRegistry 作为
artifact_store 能力注入，供 thread_report 等保存报告。

【内容摘要】
- LocalArtifactStore.save_artifact : 写入产物，返回 Artifact。
- LocalArtifactStore.get_artifact  : 按路径读取产物内容。

【职责边界】
- 只负责：产物的本地写盘与读取、Artifact 引用的构造。
- 不负责：产物的登记与全局索引（capabilities / 上层装配）、产物的对外服务
  （server）、产物在状态中的合并（state/reducers）、产物内容本身的生成（reporter）。

【INVARIANT】
- 落点固定：产物写入 {output_dir}/artifacts/ 目录。
- 目录自动创建：写入前 mkdir(parents=True, exist_ok=True)。
- artifact_id 由文件名派生：取 path.stem（不含扩展名）。
- artifact_type 固定："report_markdown"（当前实现仅面向报告产物）。
- summary 来自 metadata：metadata["summary"]，缺省为空串。
- created_at 用 utc_now_iso()：与 journal / runtime 的时间戳一致。
- 无显式接口/协议：本类为鸭子类型实现，CapabilityRegistry 按约定调用。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from poirot.backend.agents.journal.events import utc_now_iso
from poirot.backend.agents.state.types import Artifact


class LocalArtifactStore:
    """本地产物存储。

    实现 artifact_store 能力的本地版本：写盘到 {output_dir}/artifacts/，
    返回 Artifact 引用；并提供按路径读取。
    """

    def save_artifact(
        self,
        content: str,
        output_dir: str | Path,
        title: str,
        filename: str,
        metadata: dict[str, Any] | None = None,
    ) -> Artifact:
        """写入产物并返回 Artifact 引用。

        Args:
            content: 产物内容。
            output_dir: 输出根目录（产物写入其下的 artifacts/ 子目录）。
            title: 产物标题。
            filename: 产物文件名。
            metadata: 附加元数据，可选；summary 字段会被提取。

        Returns:
            Artifact: 产物记录（artifact_id 取自文件名 stem）。
        """
        artifacts_dir = Path(output_dir) / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        path = artifacts_dir / filename
        path.write_text(content, encoding="utf-8")
        artifact_id = path.stem
        return Artifact(
            artifact_id=artifact_id,
            artifact_type="report_markdown",
            title=title,
            path=str(path),
            summary=(metadata or {}).get("summary", ""),
            created_at=utc_now_iso(),
        )

    def get_artifact(self, path: str | Path) -> str:
        """按路径读取产物内容。

        Args:
            path: 产物路径。

        Returns:
            str: 产物文本内容。
        """
        return Path(path).read_text(encoding="utf-8")