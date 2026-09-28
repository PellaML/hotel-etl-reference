from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, timedelta

import pytest

from hotel_etl.errors import SourceError, ValidationError
from hotel_etl.fixtures import synthetic_items
from hotel_etl.models import AvailabilityRow, Hotel
from hotel_etl.pipeline import collect_snapshot, sync_snapshot

STAMP = datetime(2026, 9, 27, 6, tzinfo=UTC)
START = date(2028, 2, 28)
HOTELS = (Hotel("north", ("single", "double")), Hotel("south", ("single",)))


class Source:
    def __init__(self) -> None:
        self.payloads = {
            h.hotel_id: synthetic_items(h, START, START + timedelta(days=2), STAMP.date())
            for h in HOTELS
        }

    def fetch_availability(
        self, hotel_id: str, start_date: date, end_date: date
    ) -> Iterator[dict[str, object]]:
        yield from self.payloads[hotel_id]


class Sink:
    def __init__(self) -> None:
        self.calls: list[list[AvailabilityRow]] = []

    def write(self, rows: Sequence[AvailabilityRow]) -> None:
        self.calls.append(list(rows))


def collect(source: Source, **kwargs: object) -> list[AvailabilityRow]:
    options = {"start_date": START, "horizon_days": 3, "snapshot_at": STAMP, **kwargs}
    return collect_snapshot(source, HOTELS, **options)


def test_complete_snapshot_preserves_leap_day_zeroes_and_sort_order() -> None:
    rows = collect(Source())
    assert len(rows) == 9
    assert date(2028, 2, 29) in {row.stay_date for row in rows}
    assert [row.key for row in rows] == sorted(row.key for row in rows)
    assert {row.snapshot_date for row in rows} == {STAMP.date()}
    source = Source()
    source.payloads["north"][0]["available"] = 0
    assert any(row.available_rooms == 0 for row in collect(source))


def test_identical_duplicates_collapse_but_conflicting_duplicates_fail() -> None:
    source = Source()
    source.payloads["north"].append(dict(source.payloads["north"][0]))
    assert len(collect(source)) == 9
    source.payloads["north"][-1]["available"] = 123
    with pytest.raises(ValidationError, match="conflicting duplicate"):
        collect(source)


@pytest.mark.parametrize(
    "field,value",
    [
        ("room_type_id", "unknown"),
        ("room_type_id", None),
        ("date", "2028-03-02"),
        ("date", "2028-02-27"),
        ("date", "2026-02-29"),
        ("available", True),
        ("available", -1),
        ("available", 1.5),
        ("available", "3"),
        ("hotel_id", "different"),
    ],
)
def test_bad_record_aborts_every_hotel_before_writing(field: str, value: object) -> None:
    source = Source()
    source.payloads["south"][0][field] = value
    sink = Sink()
    with pytest.raises(ValidationError):
        sync_snapshot(source, sink, HOTELS, start_date=START, horizon_days=3, snapshot_at=STAMP)
    assert not sink.calls


def test_missing_record_is_not_interpreted_as_zero_availability() -> None:
    source = Source()
    source.payloads["south"].pop()
    with pytest.raises(ValidationError, match="Incomplete"):
        collect(source)


def test_read_failure_cannot_commit_a_partial_run() -> None:
    class Broken(Source):
        def fetch_availability(
            self, hotel_id: str, start_date: date, end_date: date
        ) -> Iterator[dict[str, object]]:
            yield from super().fetch_availability(hotel_id, start_date, end_date)
            if hotel_id == "south":
                raise SourceError("simulated page failure")

    sink = Sink()
    with pytest.raises(SourceError):
        sync_snapshot(Broken(), sink, HOTELS, start_date=START, horizon_days=3, snapshot_at=STAMP)
    assert not sink.calls


@pytest.mark.parametrize(
    "options",
    [
        {"horizon_days": 0},
        {"horizon_days": 367},
        {"horizon_days": True},
        {"max_rows": 0},
        {"max_rows": True},
        {"max_rows": 8},
        {"start_date": datetime(2026, 9, 27)},
        {"start_date": date(9999, 12, 31)},
    ],
)
def test_invalid_run_configuration(options: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        collect(Source(), **options)


@pytest.mark.parametrize("hotels", [(), (HOTELS[0], HOTELS[0]), ({},)])
def test_invalid_hotel_set(hotels: object) -> None:
    with pytest.raises(ValidationError):
        collect_snapshot(Source(), hotels, start_date=START, horizon_days=3, snapshot_at=STAMP)


def test_raw_record_limit_includes_identical_duplicates() -> None:
    source = Source()
    source.payloads["north"] += [source.payloads["north"][0]] * 10
    with pytest.raises(ValidationError, match="record limit"):
        collect(source, max_rows=10)


def test_success_writes_once_after_validation() -> None:
    sink = Sink()
    rows = sync_snapshot(
        Source(), sink, HOTELS, start_date=START, horizon_days=3, snapshot_at=STAMP
    )
    assert sink.calls == [rows]
