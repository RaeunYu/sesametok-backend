"""루프백 HTTP 및 Unix 도메인 소켓 서버.

두 프런트엔드는 요청 핸들러와 라우트 테이블을 하나씩 공유한다. HTTP는 ``127.0.0.1``에만
바인딩한다. 소켓은 ``0600``이며, Bearer 토큰에 더해 파일시스템 권한에도 의존한다.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .context import ApiContext
from .errors import ApiError, internal, payload_too_large
from .router import Request, Response, Router

logger = logging.getLogger("sesametok_backend.api.server")

MAX_BODY_BYTES = 2 * 1024 * 1024
SOCKET_MODE = 0o600


class ApiRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "sesametok-backend"
    sys_version = ""

    # ------------------------------------------------------------ 공통 처리

    def address_string(self) -> str:
        client = self.client_address
        return client if isinstance(client, str) else str(client[0])

    def log_message(self, fmt: str, *args) -> None:
        # 요청 라인에 chat id가 들어갈 수 있으니 debug 레벨로 유지한다.
        logger.debug("api %s", fmt % args)

    def do_GET(self) -> None:  # noqa: N802 - 표준 라이브러리 명명
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 - 표준 라이브러리 명명
        self._dispatch("POST")

    def do_PATCH(self) -> None:  # noqa: N802 - 표준 라이브러리 명명
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802 - 표준 라이브러리 명명
        self._dispatch("DELETE")

    def __getattr__(self, name: str):
        """다른 모든 HTTP 메서드를 라우터를 거쳐 보낸다(그래서 405를 응답할 수 있다).

        ``BaseHTTPRequestHandler``는 ``do_<METHOD>``를 동적으로 찾는다. 이게 없으면
        지원하지 않는 메서드가 우리 JSON 오류 계약 대신 표준 라이브러리의 HTML 501을
        받게 된다.
        """

        if name.startswith("do_"):
            method = name[3:]
            return lambda: self._dispatch(method)
        raise AttributeError(name)

    # ------------------------------------------------------------ 디스패치

    def _dispatch(self, method: str) -> None:
        context: ApiContext = self.server.context  # type: ignore[attr-defined]
        router: Router = self.server.router  # type: ignore[attr-defined]

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY_BYTES:
            self._respond(Response.json(payload_too_large().to_payload(), 413))
            return

        body = self.rfile.read(length) if length > 0 else b""
        request = Request(
            method=method, target=self.path, headers=dict(self.headers), body=body
        )

        try:
            handler, params = router.match(method, request.path)
            request.params = params
            response = handler(context, request)
        except ApiError as exc:
            response = Response.json(exc.to_payload(), exc.status)
        except Exception as exc:  # 클라이언트에 트레이스백을 절대 흘리지 않는다
            logger.error("처리되지 않은 API 오류: %s", type(exc).__name__)
            response = Response.json(internal().to_payload(), 500)

        self._respond(response)

    def _respond(self, response: Response) -> None:
        if response.stream is not None:
            headers = {
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "close",
                "X-Accel-Buffering": "no",
            }
            headers.update(response.headers)
            self.send_response(response.status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.close_connection = True
            try:
                response.stream(self.wfile)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return

        if response.status == 204 or response.body is None:
            self.send_response(response.status)
            self.send_header("Content-Length", "0")
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.end_headers()
            return

        payload = json.dumps(response.body, ensure_ascii=False).encode("utf-8")
        self.send_response(response.status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        for name, value in response.headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)


class ApiHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], context: ApiContext, router: Router) -> None:
        self.context = context
        self.router = router
        super().__init__(address, ApiRequestHandler)


class ApiUnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path: Path, context: ApiContext, router: Router) -> None:
        self.context = context
        self.router = router
        # BaseHTTPRequestHandler는 서버 객체에 이 속성들이 있기를 기대한다.
        self.server_name = "localhost"
        self.server_port = 0
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
        super().__init__(str(path), ApiRequestHandler)
        os.chmod(path, SOCKET_MODE)


MAX_SOCKET_PATH_BYTES = 100


class ApiService:
    """HTTP와 (선택적) UDS 프런트엔드를 데몬 스레드로 실행한다."""

    def __init__(
        self,
        context: ApiContext,
        router: Router,
        *,
        host: str,
        port: int,
        uds_path: Path | None = None,
    ) -> None:
        self.context = context
        self.router = router
        self.host = host
        self.port = port
        self.uds_path = uds_path
        self._http: ApiHttpServer | None = None
        self._unix: ApiUnixServer | None = None
        self._threads: list[threading.Thread] = []

    # -------------------------------------------------------------- 시작

    def start(self) -> None:
        self._http = ApiHttpServer((self.host, self.port), self.context, self.router)
        self.port = self._http.server_address[1]
        self._spawn(self._http, f"api-http:{self.host}:{self.port}")
        logger.info("API가 http://%s:%s에서 수신 대기 중", self.host, self.port)

        if self.uds_path is not None:
            encoded = str(self.uds_path).encode("utf-8")
            if len(encoded) > MAX_SOCKET_PATH_BYTES:
                # macOS에서 sun_path는 짧다. 데이터 디렉터리가 깊으면 소켓을 담을 수 없다.
                logger.warning(
                    "unix 소켓을 건너뜁니다: 경로가 %d바이트입니다(한도 ~%d); "
                    "SESAMETOK_UDS_PATH를 더 짧은 경로로 설정하세요",
                    len(encoded),
                    MAX_SOCKET_PATH_BYTES,
                )
                self.uds_path = None
            else:
                self.uds_path.parent.mkdir(parents=True, exist_ok=True)
                self._unix = ApiUnixServer(self.uds_path, self.context, self.router)
                self._spawn(self._unix, f"api-uds:{self.uds_path}")
                logger.info("API가 unix 소켓 %s에서 수신 대기 중", self.uds_path)

    def _spawn(self, server: socketserver.BaseServer, name: str) -> None:
        thread = threading.Thread(target=server.serve_forever, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    # --------------------------------------------------------------- 종료

    def stop(self) -> None:
        for server in (self._http, self._unix):
            if server is not None:
                server.shutdown()
                server.server_close()
        for thread in self._threads:
            thread.join(timeout=5)
        self._threads.clear()
        if self._unix is not None and self.uds_path is not None:
            try:
                self.uds_path.unlink()
            except OSError:
                pass
        self._http = None
        self._unix = None

    @property
    def base_url(self) -> str:
        host = self.host
        if ":" in host:  # IPv6 리터럴
            return f"http://[{host}]:{self.port}"
        return f"http://{host}:{self.port}"


def build_api_service(context: ApiContext, *, include_uds: bool = True) -> ApiService:
    from .routes import build_router

    uds_path = context.settings.uds_path if include_uds and context.settings.uds_enabled else None
    return ApiService(
        context,
        build_router(context),
        host=context.settings.host,
        port=context.settings.port,
        uds_path=uds_path,
    )
