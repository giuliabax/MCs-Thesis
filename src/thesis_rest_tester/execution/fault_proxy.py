"""An HTTP proxy that sits between a generated suite and its service, corrupting one
response class at a time.

The suite is pointed at the proxy through ``SUT_BASE_URL``, so nothing in the generated
code changes and no student application is modified. The proxy forwards every request
untouched, and rewrites only the responses the active fault matches -- which is what makes
"the suite ran against a broken service" a controlled condition rather than an accident.

It runs in-process, on a loopback port the operating system chooses, for the duration of
one suite run. There is no need for it to be fast: a suite makes tens of requests, and the
service it forwards to is a container on the same machine.
"""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

import requests

from thesis_rest_tester.evaluation.faults import SeededFault

_logger = logging.getLogger(__name__)

# Headers that describe the *forwarded* body and must not be copied verbatim: a mutated
# body has a different length, and an encoding negotiated with the upstream has already
# been undone by the client library.
_DROPPED_RESPONSE_HEADERS = frozenset(
    {"content-length", "transfer-encoding", "content-encoding", "connection"}
)
_DROPPED_REQUEST_HEADERS = frozenset({"host", "content-length", "connection", "accept-encoding"})


class FaultProxy:
    """Forward to ``upstream``, applying ``fault`` to the responses it matches.

    Used as a context manager; ``base_url`` is what the suite should be given.
    """

    def __init__(self, upstream: str, fault: SeededFault | None = None) -> None:
        self._upstream = upstream.rstrip("/")
        self._fault = fault
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        # Whether the fault ever matched anything. A fault the suite never reaches is not
        # an undetected fault, and reporting the two the same way would understate the
        # suite by counting endpoints it was never asked to exercise.
        self.applied_count = 0

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("the proxy is not running")
        host, port = self._server.server_address[:2]
        prefix = urlsplit(self._upstream).path
        return f"http://{host}:{port}{prefix}"

    def __enter__(self) -> FaultProxy:
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_: Any) -> None:  # pragma: no cover - silences stderr
                pass

            def _handle(self) -> None:
                proxy._forward(self)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _handle

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None

    # --- forwarding -------------------------------------------------------------------

    def _forward(self, handler: BaseHTTPRequestHandler) -> None:
        origin = f"{urlsplit(self._upstream).scheme}://{urlsplit(self._upstream).netloc}"
        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length) if length else None
        headers = {
            key: value
            for key, value in handler.headers.items()
            if key.lower() not in _DROPPED_REQUEST_HEADERS
        }

        try:
            upstream = requests.request(
                handler.command,
                f"{origin}{handler.path}",
                data=body,
                headers=headers,
                timeout=60,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            # The service itself failed to answer. Reported as a gateway error rather
            # than swallowed, so it cannot be mistaken for a seeded fault.
            _logger.warning("Proxy could not reach the service: %s", exc)
            handler.send_response(502)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return

        status, payload = upstream.status_code, upstream.content
        if self._fault is not None and self._fault.match.applies(
            handler.command, handler.path, upstream.status_code
        ):
            status, payload = self._fault.apply(status, payload)
            self.applied_count += 1

        handler.send_response(status)
        for key, value in upstream.headers.items():
            if key.lower() not in _DROPPED_RESPONSE_HEADERS:
                handler.send_header(key, value)
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        if handler.command != "HEAD" and payload:
            handler.wfile.write(payload)
