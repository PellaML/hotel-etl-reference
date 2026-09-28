"""HTTP framing checks and bounded reads."""

from __future__ import annotations

import re
from email.message import Message
from typing import Protocol

from hotel_etl.errors import SourceError

_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


class ResponseBody(Protocol):
    def read1(self, size: int = -1) -> bytes: ...


def validate_framing(headers: Message, limit: int) -> int | None:
    """Reject ambiguous framing; defer canonical chunk decoding to the HTTP client.

    Repeated lengths (even identical), folded headers, and transfer codings other
    than a single canonical chunked value are intentionally not supported. No
    length/transfer header means an EOF-delimited body, still subject to the cap.
    """
    if headers.defects or any(
        _HEADER_NAME.fullmatch(name) is None
        or any((ord(ch) < 32 and ch != "\t") or ord(ch) == 127 for ch in value)
        for name, value in headers.raw_items()
    ):
        raise SourceError("API response contains malformed HTTP headers.")
    lengths = headers.get_all("Content-Length", [])
    transfers = headers.get_all("Transfer-Encoding", [])
    if len(lengths) > 1 or len(transfers) > 1 or (lengths and transfers):
        raise SourceError("API response contains ambiguous HTTP framing headers.")
    if transfers:
        # Match http.client's decoder selection exactly; accepting additional
        # whitespace here could treat an undecoded body as valid JSON.
        if transfers[0].lower() != "chunked":
            raise SourceError("API response uses unsupported or malformed Transfer-Encoding.")
        return None
    if not lengths:
        return None
    digits = lengths[0].strip(" \t")
    if not digits or not digits.isascii() or not digits.isdecimal():
        raise SourceError("API response contains an invalid Content-Length.")
    digits = digits.lstrip("0") or "0"
    maximum = str(limit)
    # Compare before int conversion: a header can contain thousands of digits.
    if len(digits) > len(maximum) or (len(digits) == len(maximum) and digits > maximum):
        raise SourceError("API response exceeded the configured size limit.")
    return int(digits)


def read_bounded_body(response: ResponseBody, limit: int) -> bytes:
    """Read to the framing boundary without permitting an unbounded chunk read."""
    body = bytearray()
    while len(body) <= limit:
        # read1 bounds each underlying buffered read, including malformed chunk
        # sizes that http.client.read(n) can pass through to an unbounded read.
        piece = response.read1(min(65_536, limit + 1 - len(body)))
        if not piece:
            return bytes(body)
        body.extend(piece)
    raise SourceError("API response exceeded the configured size limit.")
