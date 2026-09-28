"""Real-loopback CLI regressions for framing integrity, retries, and token redaction."""

from __future__ import annotations

import json
import socketserver
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from hotel_etl import cli
from hotel_etl.api import HttpAvailabilityClient

TOKEN = "synthetic-transport-redaction-canary"
FAULTS = ("reflected_status", "chunked_eof", "length_eof")


def _page(room: str = "double", cursor: str | None = None) -> bytes:
    return json.dumps(
        {
            "items": [{"room_type_id": room, "date": "2026-09-27", "available": 4}],
            "next_cursor": cursor,
        }
    ).encode()


BODY = _page()


def _response(headers: Sequence[bytes], body: bytes) -> bytes:
    lines = [
        b"HTTP/1.1 200 OK",
        b"Content-Type: application/json",
        b"Connection: close",
        *headers,
    ]
    return b"\r\n".join(lines) + b"\r\n\r\n" + body


def _chunks(body: bytes) -> bytes:
    middle = len(body) // 2
    pieces = (body[:middle], body[middle:])
    return b"".join(f"{len(piece):x};demo=1\r\n".encode() + piece + b"\r\n" for piece in pieces)


def _complete(body: bytes, framing: str = "length") -> bytes:
    if framing == "chunked":
        return _response([b"Transfer-Encoding: chunked"], _chunks(body) + b"0\r\n\r\n")
    if framing == "eof":
        return _response([], body)
    return _response([f"Content-Length: {len(body)}".encode()], body)


def _broken(kind: str, authorization: str, body: bytes = BODY) -> bytes:
    if kind == "reflected_status":
        # Reflect the actual received header, not a locally substituted token.
        return f"Authorization: {authorization}\r\n\r\n".encode("ascii")
    if kind == "chunked_eof":
        # The decoded JSON would be complete, but the zero-length chunk is absent.
        return _response([b"Transfer-Encoding: chunked"], _chunks(body))
    assert kind == "length_eof"
    return _response([f"Content-Length: {len(body) + 32}".encode()], body)


@dataclass
class _Requests:
    paths: list[str] = field(default_factory=list)
    authenticated: list[bool] = field(default_factory=list)
    destination_present: list[bool] = field(default_factory=list)


@contextmanager
def _server(
    responder: Callable[[int, str], bytes], destination: Path
) -> Iterator[tuple[str, _Requests]]:
    requests = _Requests()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            self.request.settimeout(2)
            request_line = self.rfile.readline(8192)
            if not request_line:
                return
            authorization = ""
            for _ in range(64):
                line = self.rfile.readline(8192)
                if line in (b"\r\n", b"\n", b""):
                    break
                name, separator, value = line.partition(b":")
                if separator and name.lower() == b"authorization":
                    authorization = value.strip().decode("ascii")
            requests.paths.append(request_line.decode("ascii").split(" ")[1])
            requests.authenticated.append(authorization == f"Bearer {TOKEN}")
            requests.destination_present.append(destination.exists())
            response = responder(len(requests.paths), authorization)
            # Rejected headers can make the client close before the body is sent.
            with suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(response)
                self.wfile.flush()

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}", requests
        finally:
            server.shutdown()
            thread.join(timeout=3)
            assert not thread.is_alive()


def _configure_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    rooms: Sequence[str] = ("double",),
    max_response_bytes: int = 2 * 1024 * 1024,
) -> tuple[Path, list[str], list[float]]:
    configuration = tmp_path / "hotels.json"
    configuration.write_text(
        json.dumps({"hotels": [{"hotel_id": "hotel", "room_type_ids": list(rooms)}]}),
        encoding="utf-8",
    )
    database = tmp_path / "availability.sqlite"
    monkeypatch.setenv("HOTEL_API_TOKEN", TOKEN)
    sleeps: list[float] = []

    def source(base_url: str, token: str) -> HttpAvailabilityClient:
        # Keep the real urllib transport. Tests control only retry sleeps, the
        # timeout and the size limit.
        return HttpAvailabilityClient(
            base_url, token, timeout=1, sleep=sleeps.append, max_response_bytes=max_response_bytes
        )

    monkeypatch.setattr(cli, "HttpAvailabilityClient", source)
    arguments = [
        "sync",
        "--config",
        str(configuration),
        "--days",
        "1",
        "--snapshot-at",
        "2026-09-27T06:00:00Z",
        "--sqlite",
        str(database),
    ]
    return database, arguments, sleeps


def _assert_failure_output(capsys: pytest.CaptureFixture[str]) -> None:
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["status"] == "error"
    assert TOKEN not in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("kind", FAULTS)
def test_cli_transport_retry_eventually_commits_only_a_complete_response(
    kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database, arguments, sleeps = _configure_cli(tmp_path, monkeypatch)

    def respond(attempt: int, authorization: str) -> bytes:
        return _broken(kind, authorization) if attempt == 1 else _complete(BODY, "chunked")

    with _server(respond, database) as (url, requests):
        monkeypatch.setenv("HOTEL_API_BASE_URL", url)
        assert cli.main(arguments) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert TOKEN not in captured.out
    assert json.loads(captured.out)["rows_processed"] == 1
    assert len(requests.paths) == 2
    assert requests.paths[0] == requests.paths[1]
    assert requests.authenticated == [True, True]
    assert requests.destination_present == [False, False]
    assert sleeps == [0.25]
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT hotel_id, room_type_id, available_rooms FROM availability_snapshot"
        ).fetchall() == [("hotel", "double", 4)]


@pytest.mark.parametrize("kind", FAULTS)
def test_cli_transport_retry_exhaustion_is_redacted_and_never_creates_destination(
    kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database, arguments, sleeps = _configure_cli(tmp_path, monkeypatch)
    with _server(lambda _attempt, auth: _broken(kind, auth), database) as (url, requests):
        monkeypatch.setenv("HOTEL_API_BASE_URL", url)
        assert cli.main(arguments) == 1
    _assert_failure_output(capsys)
    assert len(requests.paths) == 3
    assert len(set(requests.paths)) == 1
    assert requests.authenticated == [True] * 3
    assert requests.destination_present == [False] * 3
    assert sleeps == [0.25, 0.5]
    assert not database.exists()


@pytest.mark.parametrize("kind", FAULTS)
def test_cli_transport_failure_after_a_valid_page_still_prevents_all_sink_writes(
    kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database, arguments, sleeps = _configure_cli(tmp_path, monkeypatch, rooms=("double", "single"))

    def respond(attempt: int, authorization: str) -> bytes:
        if attempt == 1:
            return _complete(_page(cursor="next-page"))
        return _broken(kind, authorization, _page("single"))

    with _server(respond, database) as (url, requests):
        monkeypatch.setenv("HOTEL_API_BASE_URL", url)
        assert cli.main(arguments) == 1
    _assert_failure_output(capsys)
    assert len(requests.paths) == 4
    assert "cursor=" not in requests.paths[0]
    assert all("cursor=next-page" in path for path in requests.paths[1:])
    assert requests.destination_present == [False] * 4
    assert sleeps == [0.25, 0.5]
    assert not database.exists()


@pytest.mark.parametrize(
    "headers",
    [
        [f"Content-Length: {len(BODY)}".encode()] * 2,
        [f"Content-Length: {len(BODY)}".encode(), f"Content-Length: {len(BODY) + 1}".encode()],
        [f"Content-Length: {len(BODY)}, {len(BODY)}".encode()],
        [b"Content-Length: +100"],
        [b"Content-Length: -1"],
        [b"Content-Length: 10.5"],
        [b"Content-Length: "],
        [b"Content-Length: \xb2"],
        [f"Content-Length: {TOKEN}".encode()],
        [f"Content-Length : {len(BODY)}".encode()],
        [f"Content-Length: {len(BODY)}\r\n 0".encode()],
        [f"Content-Length: {len(BODY)}".encode(), b"Transfer-Encoding: chunked"],
        [b"Transfer-Encoding: chunked"] * 2,
        [b"Transfer-Encoding: chunked, chunked"],
        [b"Transfer-Encoding: gzip, chunked"],
        [b"Transfer-Encoding: chunked "],
        [b"Transfer-Encoding: identity"],
        [b"X-Folded: value\r\n Content-Length: 0"],
    ],
    ids=[
        "duplicate-length",
        "conflicting-length",
        "joined-lengths",
        "signed-length",
        "negative-length",
        "fraction-length",
        "empty-length",
        "nonascii-length",
        "token-length",
        "malformed-name",
        "folded-length",
        "length-and-transfer",
        "duplicate-transfer",
        "joined-transfer",
        "unsupported-coding",
        "decoder-mismatch",
        "identity-coding",
        "folded-hidden-header",
    ],
)
def test_cli_rejects_ambiguous_or_malformed_wire_headers_before_accepting_json(
    headers: list[bytes],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database, arguments, sleeps = _configure_cli(tmp_path, monkeypatch)
    body = (
        _chunks(BODY) + b"0\r\n\r\n"
        if any(header.lower().startswith(b"transfer-encoding:") for header in headers)
        else BODY
    )
    response = _response(headers, body)
    with _server(lambda _attempt, _auth: response, database) as (url, requests):
        monkeypatch.setenv("HOTEL_API_BASE_URL", url)
        assert cli.main(arguments) == 1
    _assert_failure_output(capsys)
    assert len(requests.paths) == 1
    assert sleeps == []
    assert not database.exists()


@pytest.mark.parametrize("framing", ["length", "chunked", "eof"])
def test_cli_accepts_complete_supported_http_framing(
    framing: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database, arguments, sleeps = _configure_cli(tmp_path, monkeypatch)
    with _server(lambda _attempt, _auth: _complete(BODY, framing), database) as (url, requests):
        monkeypatch.setenv("HOTEL_API_BASE_URL", url)
        assert cli.main(arguments) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["rows_processed"] == 1
    assert len(requests.paths) == 1
    assert requests.destination_present == [False]
    assert sleeps == []


def test_malformed_negative_chunk_size_cannot_turn_the_body_limit_into_an_unbounded_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database, arguments, sleeps = _configure_cli(tmp_path, monkeypatch, max_response_bytes=128)
    response = _response([b"Transfer-Encoding: chunked"], b"-1\r\n" + b"x" * 4096)
    with _server(lambda _attempt, _auth: response, database) as (url, requests):
        monkeypatch.setenv("HOTEL_API_BASE_URL", url)
        assert cli.main(arguments) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "size limit" in json.loads(captured.err)["error"]
    assert TOKEN not in captured.err
    assert len(requests.paths) == 1
    assert sleeps == []
    assert not database.exists()
