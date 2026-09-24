"""产物下载/预览的轻量 HTTP 服务。

【整体职责】
绑定 127.0.0.1，把经 register() 登记的产物文件以 /artifacts/{sandbox_id}/{filename}
对外提供下载/预览。由 bootstrap 启动、退出时停止。无第三方依赖（stdlib http.server）。

【内容摘要】
- _DEFAULT_PORT / _MAX_PORT_RETRIES : 默认端口与端口重试次数。
- ArtifactServer                     : 本地 HTTP 产物服务。
- ArtifactServer.start / stop        : 启动 / 停止服务。
- ArtifactServer.register            : 登记产物，返回可下载 URL。
- ArtifactServer._resolve            : 按 sandbox_id + filename 查宿主路径。
- ArtifactServer._make_handler       : 构造请求处理器（GET 下载）。

【职责边界】
- 只负责：登记产物路径、按 URL 提供文件下载/预览、端口占用时自动重试。
- 不负责：产物内容的生成（reporter）、产物写盘（local_store）、服务的启动时机与
  生命周期编排（bootstrap）、访问控制与鉴权（当前实现无）。

【INVARIANT】
- 仅本机可达：绑定 127.0.0.1。
- 无第三方依赖：仅用 stdlib http.server。
- 登记制访问：只有 register 过的 {sandbox_id}/{filename} 才可下载。
- 路径穿越防护：URL 中含 ".." 或非 "/artifacts/" 前缀时拒绝（403 / 404）。
- 端口自动重试：端口被占用时逐次 +1，最多 _MAX_PORT_RETRIES 次，仍失败抛 RuntimeError。
- 后台线程运行：serve_forever 跑在 daemon 线程，stop 时 shutdown + join。
- 线程安全：_registry 读写由 _lock 保护。
- filename 可含子目录：如 workspace/x.pptx；下载名取 basename。
"""
from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 18080
_MAX_PORT_RETRIES = 10


class ArtifactServer:
    """Local HTTP server serving registered artifact files.

    Attributes:
        _host: 绑定地址。
        _port: 绑定端口（可能因占用递增）。
        _registry: {sandbox_id}/{filename} → 宿主路径 映射。
        _lock: 保护 _registry 的锁。
        _server: HTTP 服务实例。
        _thread: 服务后台线程。
    """

    def __init__(self, host: str = "127.0.0.1", port: int = _DEFAULT_PORT) -> None:
        """初始化。

        Args:
            host: 绑定地址，默认 127.0.0.1。
            port: 绑定端口，默认 18080。
        """
        self._host = host
        self._port = port
        self._registry: dict[str, str] = {}  # f"{sandbox_id}/{filename}" -> host_path
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        """服务基地址（http://host:port）。"""
        return f"http://{self._host}:{self._port}"

    def start(self) -> None:
        """Start HTTP server in background thread. Auto-increment port if occupied.

        Raises:
            RuntimeError: 连续重试 _MAX_PORT_RETRIES 次仍无法绑定端口时。
        """
        for attempt in range(_MAX_PORT_RETRIES):
            port = self._port + attempt
            try:
                server = ThreadingHTTPServer((self._host, port), self._make_handler())
                self._port = port
                self._server = server
                self._thread = threading.Thread(target=server.serve_forever, daemon=True)
                self._thread.start()
                logger.info(f"ArtifactServer listening on {self.base_url}")
                return
            except OSError:
                logger.warning(f"Port {port} occupied, trying {port + 1}")
                continue
        raise RuntimeError(f"Could not bind ArtifactServer on ports {self._port}-{self._port + _MAX_PORT_RETRIES}")

    def stop(self) -> None:
        """停止服务并回收线程。"""
        if self._server is not None:
            self._server.shutdown()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def register(self, sandbox_id: str, filename: str, host_path: str) -> str:
        """Register a host path, return downloadable URL. filename may contain subdirs.

        Args:
            sandbox_id: 沙箱 ID（URL 第一段）。
            filename: 文件名（可含子目录，如 workspace/x.pptx）。
            host_path: 宿主文件路径。

        Returns:
            str: 可下载的 URL。
        """
        key = f"{sandbox_id}/{filename}"
        with self._lock:
            self._registry[key] = host_path
        return f"{self.base_url}/artifacts/{sandbox_id}/{quote(filename, safe='')}"

    def _resolve(self, sandbox_id: str, filename: str) -> str | None:
        """按 sandbox_id + filename 查宿主路径；未登记返回 None。

        Args:
            sandbox_id: 沙箱 ID。
            filename: 文件名。

        Returns:
            str | None: 宿主路径；未登记则 None。
        """
        key = f"{sandbox_id}/{filename}"
        with self._lock:
            return self._registry.get(key)

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        """构造 HTTP 请求处理器类（处理 GET 下载）。

        Returns:
            type[BaseHTTPRequestHandler]: 处理器类。
        """
        registry_resolve = self._resolve

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args) -> None:
                logger.debug("ArtifactServer: " + fmt, *args)

            def do_GET(self) -> None:
                """处理 GET：校验路径 → 查登记 → 读文件 → 返回下载响应。"""
                path = unquote(self.path)
                if not path.startswith("/artifacts/"):
                    self.send_error(404)
                    return
                rest = path[len("/artifacts/"):]
                parts = rest.split("/", 1)
                if len(parts) != 2 or ".." in rest:
                    self.send_error(403, "path traversal blocked")
                    return
                sandbox_id = parts[0]
                filename = parts[1]  # may contain subdirs (e.g. workspace/x.pptx)
                host_path = registry_resolve(sandbox_id, filename)
                if host_path is None:
                    self.send_error(404, "artifact not registered")
                    return
                p = Path(host_path)
                if not p.exists() or not p.is_file():
                    self.send_error(404, "file not found")
                    return
                data = p.read_bytes()
                display_name = Path(filename).name
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", f'inline; filename="{display_name}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return _Handler