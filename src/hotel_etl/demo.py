"""Run the synthetic loopback demo and write its local outputs."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from hotel_etl.api import HttpAvailabilityClient
from hotel_etl.errors import PipelineError, ValidationError
from hotel_etl.fixtures import DEMO_HOTELS, DEMO_TOKEN, fixture_api
from hotel_etl.models import AvailabilityRow
from hotel_etl.pipeline import sync_snapshot
from hotel_etl.storage.sqlite import SQLiteSink


def _write_ndjson(path: Path, rows: Sequence[AvailabilityRow]) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            for row in rows:
                stream.write(json.dumps(row.to_dict(), sort_keys=True, allow_nan=False) + "\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _database_count(path: Path) -> int:
    with closing(sqlite3.connect(path)) as connection:
        return int(connection.execute("SELECT count(*) FROM availability_snapshot").fetchone()[0])


def run_demo(output: Path, *, days: int, first_date: date) -> dict[str, object]:
    if type(days) is not int or not 1 <= days <= 366:
        raise ValidationError("Demo horizon must be between 1 and 366 days.")
    try:
        second_date = first_date + timedelta(days=1)
        _ = second_date + timedelta(days=days - 1)
    except OverflowError:
        raise ValidationError("Demo range exceeds the supported calendar.") from None
    output.mkdir(parents=True, exist_ok=True)
    database = output / "availability.sqlite"
    if any(
        (output / name).exists()
        for name in ("availability.sqlite", "snapshot.ndjson", "summary.json")
    ):
        raise ValidationError(
            "Demo output already exists; choose a new directory. Existing files kept."
        )
    database.touch(exist_ok=False)
    sink = SQLiteSink(database)
    first_stamp = datetime.combine(first_date, time(6), UTC)
    with fixture_api(DEMO_HOTELS, first_date) as url:
        source = HttpAvailabilityClient(url, DEMO_TOKEN)
        first = sync_snapshot(
            source,
            sink,
            DEMO_HOTELS,
            start_date=first_date,
            horizon_days=days,
            snapshot_at=first_stamp,
        )
        first_count = _database_count(database)
        sink.write(first)  # Verify that identical replay adds no keys.
        replay_count = _database_count(database)
    with fixture_api(DEMO_HOTELS, second_date) as url:
        second = sync_snapshot(
            HttpAvailabilityClient(url, DEMO_TOKEN),
            sink,
            DEMO_HOTELS,
            start_date=second_date,
            horizon_days=days,
            snapshot_at=first_stamp + timedelta(days=1),
        )
    total = _database_count(database)
    # All demo hotels have the same room-type count; unpacking rejects a mismatch.
    (room_types_per_hotel,) = {len(hotel.room_type_ids) for hotel in DEMO_HOTELS}
    expected = len(DEMO_HOTELS) * room_types_per_hotel * days
    if first_count != expected or replay_count != expected or total != 2 * expected:
        raise PipelineError("Demo failed its row-count or replay acceptance checks.")
    _write_ndjson(output / "snapshot.ndjson", first + second)
    summary: dict[str, object] = {
        "status": "ok",
        "synthetic_data_only": True,
        "live_cloud_verified": False,
        "hotels": len(DEMO_HOTELS),
        "room_types_per_hotel": room_types_per_hotel,
        "horizon_days": days,
        "snapshot_dates": [first_date.isoformat(), second_date.isoformat()],
        "first_snapshot_rows": first_count,
        "rows_after_identical_replay": replay_count,
        "rows_after_second_day": total,
        "output_directory": str(output.resolve()),
    }
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    return summary
