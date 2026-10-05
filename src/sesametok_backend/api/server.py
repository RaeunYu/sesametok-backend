"""Loopback HTTP and Unix-domain-socket servers.

Both front-ends share one request handler and route table. HTTP binds to
``127.0.0.1`` only; the socket is ``0600`` and relies on filesystem permissions
in addition to the bearer token.
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

    # ------------------------------------------------------------ plumbing

    def address_string(self) -> str:
        client = self.client_address
        return client if isinstance(client, str) else str(client[0])

    def log_message(self, fmt: str, *args) -> None:
        # Request lines can contain chat ids; keep them at debug level.
        logger.debug("api %s", fmt % args)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def __getattr__(self, name: str):
        """Route any other HTTP method through the router (so it can answer 405).

        ``BaseHTTPRequestHandler`` looks up ``do_<METHOD>`` dynamically; without
        this, an unsupported verb would get the stdlib's HTML 501 instead of our
        JSON error contract.
        """

        if name.startswith("do_"):
            method = name[3:]
            return lambda: self._dispatch(method)
        raise AttributeError(name)

    # ------------------------------------------------------------ dispatch

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
        except Exception as exc:  # never leak a traceback to the client
            logger.error("unhandled API error: %s", type(exc).__name__)
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
        # BaseHTTPRequestHandler expects these on the server object.
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
    """Runs the HTTP and (optionally) UDS front-ends in daemon threads."""

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

    # -------------------------------------------------------------- start

    def start(self) -> None:
        self._http = ApiHttpServer((self.host, self.port), self.context, self.router)
        self.port = self._http.server_address[1]
        self._spawn(self._http, f"api-http:{self.host}:{self.port}")
        logger.info("API listening on http://%s:%s", self.host, self.port)

        if self.uds_path is not None:
            encoded = str(self.uds_path).encode("utf-8")
            if len(encoded) > MAX_SOCKET_PATH_BYTES:
                # sun_path is short on macOS; a deep data dir cannot hold a socket.
                logger.warning(
                    "skipping the unix socket: path is %d bytes (limit ~%d); "
                    "set SESAMETOK_UDS_PATH to a shorter path",
                    len(encoded),
                    MAX_SOCKET_PATH_BYTES,
                )
                self.uds_path = None
            else:
                self.uds_path.parent.mkdir(parents=True, exist_ok=True)
                self._unix = ApiUnixServer(self.uds_path, self.context, self.router)
                self._spawn(self._unix, f"api-uds:{self.uds_path}")
                logger.info("API listening on unix socket %s", self.uds_path)

    def _spawn(self, server: socketserver.BaseServer, name: str) -> None:
        thread = threading.Thread(target=server.serve_forever, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    # --------------------------------------------------------------- stop

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
        if ":" in host:  # IPv6 literal
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
