"""Artifact services — 产物服务模块。

【整体职责】
负责产物的存储与对外提供：本地产物写盘/读取，以及通过轻量 HTTP 服务提供
产物的下载/预览。供 CapabilityRegistry 作为 artifact_store 能力注入，
供 thread_report 等保存报告。

【内容摘要】
- local_store : 本地产物存储（LocalArtifactStore），save_artifact 写盘 + get_artifact 读取。
- server      : 产物 HTTP 服务（ArtifactServer），登记并对外提供产物下载/预览。

【职责边界】
- 只负责：产物的本地写盘/读取、产物的 HTTP 对外提供。
- 不负责：产物内容的生成（reporter）、产物在状态中的合并（state/reducers）、
  服务的启动与生命周期编排（bootstrap）、全局产物索引（capabilities）。

【分工】
- local_store ：产物"怎么存"（写入 {output_dir}/artifacts/，返回 Artifact 引用）。
- server      ：产物"怎么发"（register 登记路径，GET /artifacts/{sandbox_id}/{filename} 下载）。
"""
from poirot.backend.agents.artifacts.server import ArtifactServer

__all__ = ["ArtifactServer"]