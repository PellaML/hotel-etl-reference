"""Strict JSON decoding shared by the API and configuration reader."""

from __future__ import annotations

import json
from typing import Any, NoReturn


def _reject_constant(_value: str) -> NoReturn:
    raise ValueError("Nonstandard JSON constant")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def loads_strict(data: bytes) -> object:
    """Reject duplicate keys and nonstandard constants rather than guessing intent."""
    try:
        return json.loads(
            data.decode("utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (UnicodeError, ValueError, RecursionError):
        raise ValueError("Invalid UTF-8 JSON document.") from None
