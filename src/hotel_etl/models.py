"""Strict canonical records shared by local and cloud adapters."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

from hotel_etl.errors import ValidationError

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z", re.ASCII)
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
MAX_INT64 = 2**63 - 1


def validate_id(value: object, field: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValidationError(f"{field} must be a 1-128 character identifier, not a display name.")
    return value


def parse_date(value: object) -> date:
    if not isinstance(value, str) or _DATE.fullmatch(value) is None:
        raise ValidationError("Date must use YYYY-MM-DD format.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValidationError("Date does not exist in the calendar.") from None


def utc_timestamp(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("Observation time must include a timezone.")
    try:
        return value.astimezone(UTC)
    except (OverflowError, ValueError):
        raise ValidationError("Observation time is outside the supported UTC range.") from None


@dataclass(frozen=True, slots=True)
class Hotel:
    hotel_id: str
    room_type_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        validate_id(self.hotel_id, "hotel_id")
        if not isinstance(self.room_type_ids, tuple) or not self.room_type_ids:
            raise ValidationError("Each hotel needs a nonempty tuple of expected room-type IDs.")
        for room in self.room_type_ids:
            validate_id(room, "room_type_id")
        if len(set(self.room_type_ids)) != len(self.room_type_ids):
            raise ValidationError("Room-type IDs must be unique within a hotel.")


@dataclass(frozen=True, slots=True)
class AvailabilityRow:
    hotel_id: str
    room_type_id: str
    stay_date: date
    available_rooms: int
    snapshot_date: date
    observed_at: datetime

    def __post_init__(self) -> None:
        validate_id(self.hotel_id, "hotel_id")
        validate_id(self.room_type_id, "room_type_id")
        if type(self.stay_date) is not date or type(self.snapshot_date) is not date:
            raise ValidationError("Stay and snapshot dates must be calendar dates, not timestamps.")
        if type(self.available_rooms) is not int or not 0 <= self.available_rooms <= MAX_INT64:
            raise ValidationError("available_rooms must be a nonnegative int64, not a boolean.")
        normalized = utc_timestamp(self.observed_at)
        if normalized.date() != self.snapshot_date:
            raise ValidationError("snapshot_date must equal the UTC observation date.")
        object.__setattr__(self, "observed_at", normalized)

    @property
    def key(self) -> tuple[str, str, date, date]:
        return (self.hotel_id, self.room_type_id, self.snapshot_date, self.stay_date)

    @property
    def observed_at_text(self) -> str:
        return self.observed_at.isoformat(timespec="microseconds").replace("+00:00", "Z")

    def to_dict(self) -> dict[str, object]:
        return {
            "hotel_id": self.hotel_id,
            "room_type_id": self.room_type_id,
            "stay_date": self.stay_date.isoformat(),
            "available_rooms": self.available_rooms,
            "snapshot_date": self.snapshot_date.isoformat(),
            "observed_at": self.observed_at_text,
        }


def validate_batch(rows: Sequence[AvailabilityRow]) -> None:
    seen: set[tuple[str, str, date, date]] = set()
    for row in rows:
        if not isinstance(row, AvailabilityRow):
            raise ValidationError("Sink batches must contain AvailabilityRow records.")
        key = row.key
        if key in seen:
            raise ValidationError("Sink batch contains a duplicate snapshot key.")
        seen.add(key)
