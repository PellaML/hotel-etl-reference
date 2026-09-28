from __future__ import annotations

import io
import json
import threading
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from email.errors import MissingHeaderBodySeparatorDefect
from email.message import Message
from http.client import BadStatusLine, HTTPException, IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from hotel_etl.api import HttpAvailabilityClient, decode_response_json, retry_delay
from hotel_etl.errors import SourceError, ValidationError
from hotel_etl.fixtures import DEMO_HOTELS, DEMO_TOKEN, fixture_api

START = date(2026, 9, 27)
TOKEN = "do-not-print-this-test-token"


class Response(io.BytesIO):
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        mime: str = "application/json",
        headers: Sequence[tuple[str, str]] = (),
    ) -> None:
        super().__init__(body)
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = mime
        for name, value in headers:
            self.headers[name] = value
        self.read_sizes: list[int] = []

    def read1(self, size: int | None = -1) -> bytes:
        self.read_sizes.append(-1 if size is None else size)
        return super().read1(size)


class Opener:
    def __init__(self, *results: Response | Exception) -> None:
        self.results = list(results)
        self.requests: list[Any] = []
        self.timeouts: list[float] = []

    def open(self, request: Any, *, timeout: float) -> Response:
        self.requests.append(request)
        self.timeouts.append(timeout)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def page(items: list[object] | None = None, cursor: object = None) -> Response:
    return Response(json.dumps({"items": items or [], "next_cursor": cursor}).encode())


def fetch(opener: Opener, **kwargs: Any) -> list[dict[str, object]]:
    client = HttpAvailabilityClient(
        "https://api.example.invalid/v1", TOKEN, opener=opener, **kwargs
    )
    return list(client.fetch_availability("north", START, START))


def test_actual_paginated_loopback_http_contract() -> None:
    with fixture_api(DEMO_HOTELS, START, page_size=2) as url:
        rows = list(
            HttpAvailabilityClient(url, DEMO_TOKEN).fetch_availability(
                "DEMO_NORTH", START, START + timedelta(days=1)
            )
        )
    assert len(rows) == 6
    assert {row["room_type_id"] for row in rows} == {"single", "double", "suite"}


def test_opaque_cursor_cannot_change_request_origin_or_path() -> None:
    cursor = "https://elsewhere.invalid/steal?x=1&y=2"
    opener = Opener(
        page([{"room_type_id": "single", "date": "2026-09-27", "available": 3}], cursor), page()
    )
    rows = fetch(opener, timeout=4)
    assert len(rows) == 1
    assert len(opener.requests) == 2
    target = urlsplit(opener.requests[1].full_url)
    assert target.hostname == "api.example.invalid"
    assert target.path == "/v1/availability"
    assert parse_qs(target.query)["cursor"] == [cursor]
    assert TOKEN not in target.geturl()
    assert opener.requests[0].get_header("Authorization") == f"Bearer {TOKEN}"
    assert opener.timeouts == [4, 4]


@pytest.mark.parametrize(
    "base",
    [
        "http://example.invalid",
        "ftp://example.invalid",
        "https://user:pass@example.invalid",
        "https://example.invalid?token=secret",
        "https://example.invalid#fragment",
        "https://example.invalid/v1?",
        "https://example.invalid/v1#",
        "https://example.invalid?#",
        "https://",
        "https://example.invalid:99999",
        "https://exa mple.invalid",
        "https://[invalid",
        "http://127.0.0.1.evil.invalid",
        "http://10.0.0.1",
        "http://169.254.169.254",
    ],
)
def test_unsafe_url_rejected_before_request(base: str) -> None:
    opener = Opener()
    with pytest.raises(ValidationError):
        HttpAvailabilityClient(base, TOKEN, opener=opener)
    assert not opener.requests


@pytest.mark.parametrize(
    "base",
    [
        "https://api.example.invalid",
        "http://localhost:1234",
        "http://127.0.0.1:1234",
        "http://[::1]:1234",
    ],
)
def test_secure_and_loopback_urls_allowed(base: str) -> None:
    HttpAvailabilityClient(base, TOKEN)


@pytest.mark.parametrize("proxy_source", ["environment", "system"])
def test_default_loopback_opener_ignores_proxy_settings(
    proxy_recorder: tuple[str, list[tuple[str, str, str]]],
    monkeypatch: pytest.MonkeyPatch,
    proxy_source: str,
) -> None:
    proxy_url, proxied = proxy_recorder
    if proxy_source == "environment":
        monkeypatch.setenv("HTTP_PROXY", proxy_url)
        monkeypatch.setenv("HTTPS_PROXY", proxy_url)
    else:
        # Stands in for a Windows registry proxy whose bypass list omits 127.0.0.1.
        proxies = {"http": proxy_url, "https": proxy_url}
        monkeypatch.setattr(urllib.request, "getproxies", lambda: proxies)
        monkeypatch.setattr(urllib.request, "proxy_bypass", lambda _host: False)
    with fixture_api(DEMO_HOTELS, START) as url:
        client = HttpAvailabilityClient(url, DEMO_TOKEN)
        rows = list(client.fetch_availability("DEMO_NORTH", START, START))
    assert len(rows) == 3
    assert proxied == []


@pytest.mark.parametrize("variables", [("HTTPS_PROXY",), ("HTTPS_PROXY", "HTTP_PROXY")])
def test_remote_https_retries_keep_a_port_443_proxy_tunnel_without_giving_it_the_token(
    proxy_recorder: tuple[str, list[tuple[str, str, str]]],
    monkeypatch: pytest.MonkeyPatch,
    variables: tuple[str, ...],
) -> None:
    proxy_url, proxied = proxy_recorder
    for variable in variables:
        monkeypatch.setenv(variable, proxy_url)
    sleeps: list[float] = []
    client = HttpAvailabilityClient("https://api.example.invalid/v1", TOKEN, sleep=sleeps.append)
    with pytest.raises(SourceError, match="bounded retries"):
        list(client.fetch_availability("north", START, START))
    assert [(method, target) for method, target, _ in proxied] == [
        ("CONNECT", "api.example.invalid:443")
    ] * 3
    assert TOKEN not in repr(proxied)
    assert sleeps == [0.25, 0.5]


def test_each_attempt_gets_a_fresh_request() -> None:
    opener = Opener(http_error(503), http_error(503), page())
    assert fetch(opener, sleep=lambda _delay: None) == []
    assert len({id(request) for request in opener.requests}) == 3


def test_injected_opener_is_kept_for_loopback_urls() -> None:
    opener = Opener(page())
    client = HttpAvailabilityClient("http://127.0.0.1:9/v1", TOKEN, opener=opener)
    assert list(client.fetch_availability("north", START, START)) == []
    assert urlsplit(opener.requests[0].full_url).netloc == "127.0.0.1:9"


@pytest.mark.parametrize("token", ["", " ", "abc\r\nInjected: yes", "nonascii-ł"])
def test_invalid_token_error_never_echoes_token(token: str) -> None:
    with pytest.raises(ValidationError) as error:
        HttpAvailabilityClient("https://api.example.invalid", token)
    if token.strip():
        assert token not in str(error.value)


@pytest.mark.parametrize(
    "config",
    [
        {"timeout": 0},
        {"timeout": float("nan")},
        {"timeout": float("inf")},
        {"timeout": True},
        {"max_attempts": 0},
        {"max_attempts": 11},
        {"max_attempts": True},
        {"max_pages": 0},
        {"max_response_bytes": 0},
        {"max_items": 0},
    ],
)
def test_invalid_limits(config: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        HttpAvailabilityClient("https://api.example.invalid", TOKEN, **cast(dict[str, Any], config))


@pytest.mark.parametrize(
    "body", [b"", b"{", b"\xff", b'{"a": 1, "a": 2}', b'{"x": NaN}', b'{"x": Infinity}']
)
def test_response_json_rejects_ambiguous_or_malformed_input(body: bytes) -> None:
    with pytest.raises(SourceError):
        decode_response_json(body)


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        b"null",
        b"{}",
        b'{"items": []}',
        b'{"items": {}, "next_cursor": null}',
        b'{"items": [1], "next_cursor": null}',
        b'{"items": [], "next_cursor": false}',
        b'{"items": [], "next_cursor": ""}',
    ],
)
def test_invalid_response_shape(body: bytes) -> None:
    with pytest.raises(SourceError):
        fetch(Opener(Response(body)))


def test_non_json_and_partial_responses_rejected() -> None:
    with pytest.raises(SourceError, match="not JSON"):
        fetch(Opener(Response(TOKEN.encode(), mime="text/html")))
    with pytest.raises(SourceError, match="complete HTTP 200"):
        fetch(Opener(Response(b"{}", status=206)))
    assert (
        fetch(
            Opener(
                Response(b'{"items": [], "next_cursor": null}', mime="application/vnd.example+json")
            )
        )
        == []
    )


def test_response_size_limit_and_exact_boundary() -> None:
    body = b'{"items": [], "next_cursor": null}'
    with pytest.raises(SourceError, match="size limit"):
        fetch(Opener(Response(body)), max_response_bytes=len(body) - 1)
    assert fetch(Opener(Response(body)), max_response_bytes=len(body)) == []


def test_pagination_cycles_page_and_record_limits() -> None:
    with pytest.raises(SourceError, match="repeated"):
        fetch(Opener(page(cursor="a"), page(cursor="a")))
    with pytest.raises(SourceError, match="page limit"):
        fetch(Opener(page(cursor="a")), max_pages=1)
    with pytest.raises(SourceError, match="record limit"):
        fetch(Opener(page([{}, {}])), max_items=1)
    with pytest.raises(SourceError, match="bounded"):
        fetch(Opener(page(cursor="x" * 4097)))


def http_error(status: int, retry_after: str | None = None) -> HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError(
        f"https://api.example.invalid/{TOKEN}", status, TOKEN, headers, io.BytesIO(TOKEN.encode())
    )


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_transient_status_retried_then_succeeds(status: int) -> None:
    sleeps: list[float] = []
    opener = Opener(http_error(status, "1"), page())
    assert fetch(opener, sleep=sleeps.append) == []
    assert sleeps == [1.0]
    assert len(opener.requests) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_permanent_errors_fail_once_without_disclosing_token(status: int) -> None:
    opener = Opener(http_error(status))
    with pytest.raises(SourceError) as error:
        fetch(opener)
    assert len(opener.requests) == 1
    assert str(status) in str(error.value)
    assert TOKEN not in str(error.value)


def test_network_retry_budget_and_error_redaction() -> None:
    sleeps: list[float] = []
    opener = Opener(*(URLError(TOKEN) for _ in range(3)))
    with pytest.raises(SourceError, match="bounded retries") as error:
        fetch(opener, sleep=sleeps.append)
    assert len(opener.requests) == 3
    assert sleeps == [0.25, 0.5]
    assert TOKEN not in str(error.value)


def test_repeated_transient_error_stops_at_budget() -> None:
    opener = Opener(http_error(503), http_error(503))
    sleeps: list[float] = []
    with pytest.raises(SourceError, match="503"):
        fetch(opener, max_attempts=2, sleep=sleeps.append)
    assert sleeps == [0.25]


def test_retry_after_date_and_long_delay() -> None:
    now = datetime(2026, 9, 27, tzinfo=UTC)
    assert retry_delay("Sun, 27 Sep 2026 00:00:02 GMT", 0, now=now) == 2
    assert retry_delay("invalid", 0, now=now) == 0.25
    assert retry_delay("Sun, 27 Sep 2026 00:00:02", 0, now=now) == 2
    assert retry_delay("-1", 0, now=now) == 0.25
    sleeps: list[float] = []
    with pytest.raises(SourceError, match="long retry delay"):
        fetch(Opener(http_error(429, "3600")), sleep=sleeps.append)
    assert not sleeps


@contextmanager
def redirect_server(status: int) -> Iterator[tuple[str, list[str]]]:
    paths: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            paths.append(self.path)
            if self.path.startswith("/availability?"):
                self.send_response(status)
                port = cast(ThreadingHTTPServer, self.server).server_port
                self.send_header("Location", f"http://127.0.0.1:{port}/receiver")
            else:
                self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", paths
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_actual_http_redirect_is_never_followed(status: int) -> None:
    with redirect_server(status) as (url, paths):
        source = HttpAvailabilityClient(url, TOKEN)
        with pytest.raises(SourceError, match=str(status)):
            list(source.fetch_availability("north", START, START))
        assert len(paths) == 1
        assert not any("receiver" in path for path in paths)


def test_invalid_source_date_range() -> None:
    source = HttpAvailabilityClient("https://api.example.invalid", TOKEN)
    with pytest.raises(ValidationError):
        list(source.fetch_availability("north", START, START - timedelta(days=1)))


@pytest.mark.parametrize(
    "headers",
    [
        [("Content-Length", "32"), ("Content-Length", "32")],
        [("Content-Length", "32"), ("content-length", "33")],
        [("Content-Length", "32, 32")],
        [("Content-Length", "32, 33")],
        [("Content-Length", "")],
        [("Content-Length", " \t")],
        [("Content-Length", "+32")],
        [("Content-Length", "-1")],
        [("Content-Length", "32.0")],
        [("Content-Length", "3e1")],
        [("Content-Length", "²")],
        [("Content-Length", TOKEN)],
        [("Content-Length", "32\r\n 0")],
        [("Content-Length ", "32")],
        [("Content-Length", "32"), ("Transfer-Encoding", "chunked")],
        [("Transfer-Encoding", "chunked"), ("Transfer-Encoding", "chunked")],
        [("Transfer-Encoding", "")],
        [("Transfer-Encoding", "identity")],
        [("Transfer-Encoding", "gzip, chunked")],
        [("Transfer-Encoding", "chunked, chunked")],
        [("Transfer-Encoding", "chunked ")],
        [("Transfer-Encoding", TOKEN)],
        [("X-Invalid", "folded\r\n value")],
        [("X-Invalid", "nul\x00value")],
        [("X-Invalid", "del\x7fvalue")],
    ],
    ids=[
        "duplicate-length",
        "conflicting-length",
        "joined-identical-lengths",
        "joined-lengths",
        "empty-length",
        "blank-length",
        "signed-length",
        "negative-length",
        "fractional-length",
        "exponent-length",
        "nonascii-length",
        "reflected-length",
        "folded-length",
        "bad-name",
        "length-and-transfer",
        "duplicate-transfer",
        "empty-transfer",
        "identity-transfer",
        "multiple-codings",
        "repeated-chunked",
        "decoder-mismatch",
        "reflected-transfer",
        "folded-header",
        "nul-header",
        "del-header",
    ],
)
def test_ambiguous_or_malformed_headers_fail_before_read_without_retry(
    headers: list[tuple[str, str]],
) -> None:
    response = Response(b'{"items": [], "next_cursor": null}', headers=headers)
    opener = Opener(response)
    with pytest.raises(SourceError) as error:
        fetch(opener)
    assert TOKEN not in str(error.value)
    assert len(opener.requests) == 1
    assert response.read_sizes == []
    assert response.closed


def test_header_parser_defects_are_not_ignored() -> None:
    response = page()
    response.headers.defects.append(MissingHeaderBodySeparatorDefect(TOKEN))
    with pytest.raises(SourceError, match="malformed HTTP headers") as error:
        fetch(Opener(response))
    assert TOKEN not in str(error.value)
    assert response.read_sizes == []


@pytest.mark.parametrize("length", ["33", "9" * 5000])
def test_declared_oversize_is_rejected_before_integer_conversion_or_body_read(length: str) -> None:
    response = Response(b"{}", headers=[("Content-Length", length)])
    with pytest.raises(SourceError, match="size limit"):
        fetch(Opener(response), max_response_bytes=32)
    assert response.read_sizes == []


@pytest.mark.parametrize("padding", ["", "0", "0000"])
def test_valid_content_length_accepts_ows_and_leading_zeroes(padding: str) -> None:
    body = b'{"items": [], "next_cursor": null}'
    response = Response(body, headers=[("Content-Length", f" \t{padding}{len(body)}\t ")])
    assert fetch(Opener(response), max_response_bytes=len(body)) == []
    assert response.read_sizes
    assert all(0 < size <= len(body) + 1 for size in response.read_sizes)


@pytest.mark.parametrize("coding", ["chunked", "CHUNKED", "Chunked"])
def test_single_canonical_chunked_coding_is_case_insensitive(coding: str) -> None:
    # The fake represents the HTTP client's already decoded response stream.
    response = Response(
        b'{"items": [], "next_cursor": null}', headers=[("Transfer-Encoding", coding)]
    )
    assert fetch(Opener(response)) == []


@pytest.mark.parametrize(
    "make_error",
    [
        lambda: BadStatusLine(TOKEN),
        lambda: IncompleteRead(TOKEN.encode()),
        lambda: HTTPException(TOKEN),
    ],
    ids=["status-line", "incomplete-read", "protocol-error"],
)
def test_protocol_exception_retry_budget_and_redaction(
    make_error: Callable[[], HTTPException],
) -> None:
    import traceback

    opener = Opener(*(make_error() for _ in range(3)))
    sleeps: list[float] = []
    with pytest.raises(SourceError, match="bounded retries") as error:
        fetch(opener, sleep=sleeps.append)
    assert len(opener.requests) == 3
    assert sleeps == [0.25, 0.5]
    assert TOKEN not in "".join(traceback.format_exception(error.value))


def test_short_declared_body_retries_without_returning_partial_json() -> None:
    body = b'{"items": [], "next_cursor": null}'
    partial = Response(body, headers=[("Content-Length", str(len(body) + 1))])
    complete = Response(body, headers=[("Content-Length", str(len(body)))])
    opener = Opener(partial, complete)
    sleeps: list[float] = []
    assert fetch(opener, sleep=sleeps.append) == []
    assert len(opener.requests) == 2
    assert sleeps == [0.25]
    assert partial.closed and complete.closed


def test_lengthless_response_still_uses_only_bounded_buffered_reads() -> None:
    body = b'{"items": [], "next_cursor": null}' + b" " * 150_000
    response = Response(body)
    assert fetch(Opener(response), max_response_bytes=len(body)) == []
    assert len(response.read_sizes) >= 3
    assert all(0 < size <= 65_536 for size in response.read_sizes)
