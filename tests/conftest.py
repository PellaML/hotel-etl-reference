from __future__ import annotations

import ipaddress
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.fixture(autouse=True)
def forbid_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests may reach loopback fixtures only; never public APIs or cloud metadata."""
    original = socket.socket.connect

    def guarded(sock: socket.socket, address: object) -> None:
        if not isinstance(address, tuple) or not isinstance(address[0], str):
            raise AssertionError("Non-loopback networking is forbidden in this test suite")
        try:
            allowed = ipaddress.ip_address(address[0]).is_loopback
        except ValueError:
            allowed = address[0] == "localhost"
        if not allowed:
            raise AssertionError("External networking is forbidden in this test suite")
        original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def proxy_recorder(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[str, list[tuple[str, str, str]]]]:
    """A loopback stand-in for a proxy that records each request and refuses it.

    Yields the proxy URL and a list of (method, target, headers) tuples.
    """
    requests: list[tuple[str, str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *args: object) -> None:
            pass

        def refuse(self, status: int) -> None:
            requests.append((self.command, self.path, str(self.headers)))
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:
            self.refuse(502)

        def do_CONNECT(self) -> None:
            self.refuse(403)

    for key in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(key, raising=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
