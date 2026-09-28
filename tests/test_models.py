from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from hotel_etl.errors import ValidationError
from hotel_etl.models import (
    MAX_INT64,
    AvailabilityRow,
    Hotel,
    parse_date,
    utc_timestamp,
    validate_batch,
)


def valid_row() -> AvailabilityRow:
    return AvailabilityRow(
        "demo", "double", date(2026, 9, 28), 0, date(2026, 9, 27), datetime(2026, 9, 27, tzinfo=UTC)
    )


def test_canonical_json_and_immutable_record() -> None:
    row = valid_row()
    assert row.observed_at_text == "2026-09-27T00:00:00.000000Z"
    assert row.to_dict() == {
        "hotel_id": "demo",
        "room_type_id": "double",
        "stay_date": "2026-09-28",
        "available_rooms": 0,
        "snapshot_date": "2026-09-27",
        "observed_at": "2026-09-27T00:00:00.000000Z",
    }
    with pytest.raises(FrozenInstanceError):
        row.available_rooms = 1


@pytest.mark.parametrize("count", [-1, True, False, 0.5, "1", None, MAX_INT64 + 1])
def test_invalid_counts(count: object) -> None:
    with pytest.raises(ValidationError):
        replace(valid_row(), available_rooms=count)


@pytest.mark.parametrize("field", ["hotel_id", "room_type_id"])
@pytest.mark.parametrize(
    "identifier",
    [
        "",
        " leading",
        "_leading",
        "-leading",
        ".leading",
        "two words",
        "x/y",
        "a\nb",
        "x" * 129,
        1,
        None,
    ],
)
def test_invalid_identifiers(field: str, identifier: object) -> None:
    with pytest.raises(ValidationError):
        replace(valid_row(), **{field: identifier})


@pytest.mark.parametrize("identifier", ["a", "7", "a_b.c-d", "Z" + "x" * 127])
def test_identifier_boundaries_are_accepted(identifier: str) -> None:
    row = replace(valid_row(), hotel_id=identifier, room_type_id=identifier)
    assert (row.hotel_id, row.room_type_id) == (identifier, identifier)
    assert Hotel(identifier, (identifier,)).room_type_ids == (identifier,)


@pytest.mark.parametrize(
    "value", ["2026-9-1", "2026-02-29", "2026-09-27T00:00:00", "2026-W01-1", " 2026-09-27", 1, None]
)
def test_invalid_iso_date(value: object) -> None:
    with pytest.raises(ValidationError):
        parse_date(value)


def test_leap_date_and_offset_normalization() -> None:
    assert parse_date("2028-02-29") == date(2028, 2, 29)
    stamp = datetime(2026, 9, 27, 2, tzinfo=timezone(timedelta(hours=2)))
    row = replace(valid_row(), observed_at=stamp)
    assert row.observed_at == datetime(2026, 9, 27, tzinfo=UTC)
    assert row.observed_at.tzinfo is UTC
    assert replace(row, available_rooms=MAX_INT64).available_rooms == MAX_INT64


@pytest.mark.parametrize("stamp", [datetime(2026, 9, 27), None])
def test_timezone_required(stamp: object) -> None:
    with pytest.raises(ValidationError):
        utc_timestamp(stamp)


def test_timestamp_and_calendar_mismatch() -> None:
    with pytest.raises(ValidationError):
        replace(valid_row(), snapshot_date=date(2026, 9, 26))
    with pytest.raises(ValidationError):
        replace(valid_row(), stay_date=datetime(2026, 9, 28, tzinfo=UTC))
    with pytest.raises(ValidationError):
        utc_timestamp(datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1))))


@pytest.mark.parametrize("rooms", [(), ("double", "double"), ("bad room",), "double", ["double"]])
def test_invalid_room_configuration(rooms: object) -> None:
    with pytest.raises(ValidationError):
        Hotel("demo", rooms)


def test_sink_batches_do_not_silently_deduplicate() -> None:
    validate_batch([])
    validate_batch([valid_row()])
    with pytest.raises(ValidationError):
        validate_batch([valid_row(), valid_row()])
    with pytest.raises(ValidationError):
        validate_batch([{}])
