"""Read and validate local hotel configuration."""

from __future__ import annotations

from pathlib import Path

from hotel_etl.errors import ValidationError
from hotel_etl.jsonio import loads_strict
from hotel_etl.models import Hotel

_MAX_CONFIG_BYTES = 64 * 1024


def load_hotels(path: Path) -> tuple[Hotel, ...]:
    with path.open("rb") as stream:
        data = stream.read(_MAX_CONFIG_BYTES + 1)
    if len(data) > _MAX_CONFIG_BYTES:
        raise ValidationError("Hotel configuration exceeds the 64 KiB limit.")
    try:
        payload = loads_strict(data)
    except ValueError:
        raise ValidationError("Hotel configuration is not valid, unambiguous JSON.") from None
    if not isinstance(payload, dict) or set(payload) != {"hotels"}:
        raise ValidationError("Configuration must contain exactly one hotels array.")
    entries = payload["hotels"]
    if not isinstance(entries, list) or not entries:
        raise ValidationError("Configure at least one hotel.")
    hotels: list[Hotel] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"hotel_id", "room_type_ids"}:
            raise ValidationError("Each hotel requires hotel_id and room_type_ids only.")
        rooms, identifier = entry["room_type_ids"], entry["hotel_id"]
        if not isinstance(rooms, list) or not isinstance(identifier, str):
            raise ValidationError("Hotel ID must be a string and room_type_ids must be an array.")
        hotels.append(Hotel(identifier, tuple(rooms)))
    if len({hotel.hotel_id for hotel in hotels}) != len(hotels):
        raise ValidationError("Hotel IDs must be unique.")
    return tuple(hotels)
