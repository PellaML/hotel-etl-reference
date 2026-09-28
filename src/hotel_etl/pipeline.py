"""Extract and validate an entire snapshot before permitting any destination write."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import date, datetime, timedelta
from typing import Protocol

from hotel_etl.errors import ValidationError
from hotel_etl.models import AvailabilityRow, Hotel, parse_date, utc_timestamp


class AvailabilitySource(Protocol):
    def fetch_availability(
        self, hotel_id: str, start_date: date, end_date: date
    ) -> Iterator[dict[str, object]]: ...


class SnapshotSink(Protocol):
    def write(self, rows: Sequence[AvailabilityRow]) -> None: ...


def collect_snapshot(
    source: AvailabilitySource,
    hotels: Sequence[Hotel],
    *,
    start_date: date,
    horizon_days: int = 365,
    snapshot_at: datetime,
    max_rows: int = 100_000,
) -> list[AvailabilityRow]:
    stamp = utc_timestamp(snapshot_at)
    snapshot_date = stamp.date()
    if type(start_date) is not date:
        raise ValidationError("start_date must be a calendar date.")
    if type(horizon_days) is not int or not 1 <= horizon_days <= 366:
        raise ValidationError("horizon_days must be between 1 and 366.")
    if type(max_rows) is not int or not 1 <= max_rows <= 1_000_000:
        raise ValidationError("max_rows must be between 1 and 1,000,000.")
    if not hotels or any(not isinstance(hotel, Hotel) for hotel in hotels):
        raise ValidationError("Configure at least one validated hotel.")
    if len({hotel.hotel_id for hotel in hotels}) != len(hotels):
        raise ValidationError("Hotel IDs must be unique.")
    expected_total = sum(len(hotel.room_type_ids) for hotel in hotels) * horizon_days
    if expected_total > max_rows:
        raise ValidationError("Expected snapshot exceeds max_rows; split the workload explicitly.")
    try:
        end_date = start_date + timedelta(days=horizon_days - 1)
    except OverflowError:
        raise ValidationError("Availability range exceeds the supported calendar.") from None
    rows: list[AvailabilityRow] = []
    source_count = 0
    for hotel in hotels:
        unique: dict[tuple[str, date], AvailabilityRow] = {}
        expected_rooms = set(hotel.room_type_ids)
        for item in source.fetch_availability(hotel.hotel_id, start_date, end_date):
            source_count += 1
            if source_count > max_rows:
                raise ValidationError("Source record limit exceeded, including repeated records.")
            if "hotel_id" in item and item["hotel_id"] != hotel.hotel_id:
                raise ValidationError("Source returned records for the wrong hotel.")
            room = item.get("room_type_id")
            if not isinstance(room, str) or room not in expected_rooms:
                raise ValidationError("Source returned an unexpected or missing room-type ID.")
            stay = parse_date(item.get("date"))
            if not start_date <= stay <= end_date:
                raise ValidationError("Source returned a stay date outside the requested range.")
            available = item.get("available")
            # AvailabilityRow enforces this too; checking here gives source data a
            # clearer message for fractions.
            if type(available) is not int:
                raise ValidationError("Availability must be an integer, not a boolean or fraction.")
            row = AvailabilityRow(hotel.hotel_id, room, stay, available, snapshot_date, stamp)
            key = (room, stay)
            if key in unique and unique[key] != row:
                raise ValidationError("Source contains conflicting duplicate availability records.")
            unique[key] = row
        expected = len(expected_rooms) * horizon_days
        if len(unique) != expected:
            raise ValidationError(
                f"Incomplete hotel snapshot: expected {expected} unique records, got {len(unique)}."
            )
        rows.extend(unique.values())
    return sorted(rows, key=lambda row: row.key)


def sync_snapshot(
    source: AvailabilitySource,
    sink: SnapshotSink,
    hotels: Sequence[Hotel],
    *,
    start_date: date,
    horizon_days: int = 365,
    snapshot_at: datetime,
    max_rows: int = 100_000,
) -> list[AvailabilityRow]:
    rows = collect_snapshot(
        source,
        hotels,
        start_date=start_date,
        horizon_days=horizon_days,
        snapshot_at=snapshot_at,
        max_rows=max_rows,
    )
    sink.write(rows)
    return rows
