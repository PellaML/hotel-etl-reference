"""Bounded, same-origin, token-authenticated reader for the documented fixture API."""

from __future__ import annotations

import ipaddress
import math
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import BaseHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener

from hotel_etl.errors import SourceError, ValidationError
from hotel_etl.http_transport import read_bounded_body, validate_framing
from hotel_etl.jsonio import loads_strict
from hotel_etl.models import validate_id

_RETRYABLE = {408, 429, 500, 502, 503, 504}


class _NoRedirect(HTTPRedirectHandler):
    # Never forward a bearer token through a redirect.
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def decode_response_json(data: bytes) -> object:
    """Decode an API response body; malformed or ambiguous JSON is a SourceError."""
    try:
        return loads_strict(data)
    except ValueError:
        raise SourceError("Response is not valid, unambiguous UTF-8 JSON.") from None


def _parse_base_url(value: str) -> tuple[str, bool]:
    """Return the base URL without trailing slashes and whether its host is loopback."""
    try:
        parts = urlsplit(value)
        # SplitResult checks the port only when .port is read. Read it here so a
        # malformed or out-of-range port fails before any request is built.
        _ = parts.port
    except ValueError:
        raise ValidationError("Invalid API base URL.") from None
    if (
        not parts.hostname
        or parts.username is not None
        or parts.password is not None
        # Check the raw text: a bare trailing "?" or "#" parses as an empty query
        # or fragment.
        or "?" in value
        or "#" in value
        or any(ch.isspace() for ch in value)
    ):
        raise ValidationError(
            "API URL must not contain credentials, whitespace, query or fragment."
        )
    loopback = parts.hostname.lower() == "localhost"
    with suppress(ValueError):
        loopback = loopback or ipaddress.ip_address(parts.hostname).is_loopback
    if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
        raise ValidationError("API URL must use HTTPS; only loopback fixtures may use HTTP.")
    return value.rstrip("/"), loopback


def retry_delay(header: str | None, attempt: int, *, now: datetime | None = None) -> float:
    """Use Retry-After up to 30 seconds; raise rather than retry early if asked to wait longer."""
    delay = min(0.25 * 2**attempt, 4.0)
    if header:
        try:
            if header.strip().isascii() and header.strip().isdigit():
                requested = float(header.strip())
            else:
                stamp = parsedate_to_datetime(header)
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=UTC)
                requested = (stamp - (now or datetime.now(UTC))).total_seconds()
            if requested > 30:
                raise SourceError("API requested a long retry delay; retry the job later.")
            delay = max(delay, requested)
        except (ValueError, TypeError, OverflowError):
            pass
    return float(delay)


class HttpAvailabilityClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 10.0,
        max_attempts: int = 3,
        max_pages: int = 1000,
        max_response_bytes: int = 2 * 1024 * 1024,
        max_items: int = 100_000,
        sleep: Callable[[float], None] = time.sleep,
        opener: Any | None = None,
    ) -> None:
        self.base_url, loopback = _parse_base_url(base_url)
        if not token or not token.strip() or any(ord(ch) < 33 or ord(ch) == 127 for ch in token):
            raise ValidationError(
                "API token must be nonempty and must not contain whitespace or control characters."
            )
        try:
            token.encode("ascii")
        except UnicodeEncodeError:
            raise ValidationError("Bearer token must contain ASCII characters only.") from None
        if isinstance(timeout, bool) or not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ValidationError("Request timeout must be finite and between 0 and 120 seconds.")
        for name, value, limit in (
            ("max_attempts", max_attempts, 10),
            ("max_pages", max_pages, 10_000),
            ("max_response_bytes", max_response_bytes, 64 * 1024 * 1024),
            ("max_items", max_items, 1_000_000),
        ):
            if type(value) is not int or not 1 <= value <= limit:
                raise ValidationError(f"{name} is outside the documented safety bounds.")
        self._token = token
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._max_pages = max_pages
        self._max_response_bytes = max_response_bytes
        self._max_items = max_items
        self._sleep = sleep
        if opener is None:
            # Loopback requests must ignore proxy settings from the environment or the
            # operating system, such as the Windows registry. Other hosts keep urllib's
            # normal proxy support.
            handlers: list[BaseHandler] = [_NoRedirect()]
            if loopback:
                handlers.append(ProxyHandler({}))
            opener = build_opener(*handlers)
        self._opener = opener

    def _page(self, params: dict[str, str]) -> dict[str, object]:
        url = f"{self.base_url}/availability?{urlencode(params)}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "User-Agent": "hotel-etl-reference/0.1",
        }
        for attempt in range(self._max_attempts):
            # urllib's proxy handler rewrites a Request in place. Reusing it can downgrade an
            # HTTPS retry to plain HTTP and expose the bearer token, so create a fresh request.
            request = Request(url, headers=headers)
            try:
                with self._opener.open(request, timeout=self._timeout) as response:
                    if response.status != 200:
                        raise SourceError("API did not return a complete HTTP 200 response.")
                    length = validate_framing(response.headers, self._max_response_bytes)
                    mime = response.headers.get_content_type()
                    if mime != "application/json" and not mime.endswith("+json"):
                        raise SourceError("API response is not JSON content.")
                    body = read_bounded_body(response, self._max_response_bytes)
                    if length is not None and len(body) != length:
                        # Bounded reads do not themselves reject premature EOF for a
                        # Content-Length response. Never parse a partial page.
                        raise HTTPException("Incomplete HTTP response body.")
                payload = decode_response_json(body)
                if not isinstance(payload, dict):
                    raise SourceError("API page must be a JSON object.")
                return cast(dict[str, object], payload)
            except HTTPError as exc:
                status = exc.code
                after = exc.headers.get("Retry-After") if exc.headers else None
                exc.close()
                if status not in _RETRYABLE or attempt + 1 == self._max_attempts:
                    raise SourceError(f"Availability API returned HTTP {status}.") from None
                self._sleep(retry_delay(after, attempt))
            except (HTTPException, URLError, TimeoutError, ConnectionError, OSError):
                # Protocol errors can quote response bytes, including reflected
                # Authorization headers. Retry GETs, but never expose that text.
                if attempt + 1 == self._max_attempts:
                    raise SourceError(
                        "Availability API connection failed after bounded retries."
                    ) from None
                self._sleep(retry_delay(None, attempt))
        raise AssertionError("bounded request loop must return or raise")

    def fetch_availability(
        self, hotel_id: str, start_date: date, end_date: date
    ) -> Iterator[dict[str, object]]:
        validate_id(hotel_id, "hotel_id")
        if type(start_date) is not date or type(end_date) is not date or end_date < start_date:
            raise ValidationError("Availability range must be an inclusive, ascending date range.")
        params = {
            "hotel_id": hotel_id,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }
        seen_cursors: set[str] = set()
        count = 0
        for _ in range(self._max_pages):
            page = self._page(params)
            if "items" not in page or "next_cursor" not in page:
                raise SourceError("API page is missing items or next_cursor.")
            items, next_cursor = page["items"], page["next_cursor"]
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise SourceError("API items must be an array of objects.")
            count += len(items)
            if count > self._max_items:
                raise SourceError("API result exceeded the configured record limit.")
            if next_cursor is not None:
                if not isinstance(next_cursor, str) or not 1 <= len(next_cursor) <= 4096:
                    raise SourceError("API cursor must be a bounded, nonempty string or null.")
                if next_cursor in seen_cursors:
                    raise SourceError(
                        "API pagination cursor repeated; refusing an incomplete result."
                    )
                seen_cursors.add(next_cursor)
            yield from cast(list[dict[str, object]], items)
            if next_cursor is None:
                return
            params["cursor"] = next_cursor
        raise SourceError("API pagination exceeded the configured page limit.")
