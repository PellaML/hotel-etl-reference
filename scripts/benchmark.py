"""Reproducible local normalization/SQLite benchmark; NOT a BigQuery/API throughput claim."""

from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import statistics
import tempfile
import time
import tracemalloc
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from hotel_etl.models import Hotel
from hotel_etl.pipeline import collect_snapshot
from hotel_etl.storage.sqlite import SQLiteSink

ROOM_TYPES = ("single", "double", "suite")
HORIZON_DAYS = 365


class SyntheticSource:
    def fetch_availability(
        self, hotel_id: str, start_date: date, end_date: date
    ) -> Iterator[dict[str, object]]:
        for room in ROOM_TYPES:
            for day in range((end_date - start_date).days + 1):
                yield {
                    "room_type_id": room,
                    "date": (start_date + timedelta(days=day)).isoformat(),
                    "available": day % 12,
                }


def benchmark_case(hotel_count: int, repeats: int, parent: Path) -> dict[str, object]:
    measurements: list[dict[str, float]] = []
    hotels = tuple(Hotel(f"DEMO_{number:03}", ROOM_TYPES) for number in range(hotel_count))
    expected = hotel_count * len(ROOM_TYPES) * HORIZON_DAYS
    for _ in range(repeats):
        with tempfile.TemporaryDirectory(prefix="owned-benchmark-", dir=parent) as temporary:
            # TemporaryDirectory deletes only the new directory it created. The check
            # below confirms that directory is inside the requested parent.
            directory = Path(temporary).resolve()
            if not directory.is_relative_to(parent.resolve()):
                raise RuntimeError("Unexpected temporary directory location")
            sink = SQLiteSink(directory / "benchmark.sqlite")
            tracemalloc.start()
            before = time.perf_counter()
            rows = collect_snapshot(
                SyntheticSource(),
                hotels,
                start_date=date(2026, 9, 27),
                horizon_days=HORIZON_DAYS,
                snapshot_at=datetime(2026, 9, 27, 6, tzinfo=UTC),
            )
            collected = time.perf_counter()
            sink.write(rows)
            inserted = time.perf_counter()
            sink.write(rows)
            replayed = time.perf_counter()
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            with closing(sqlite3.connect(sink.database)) as connection:
                actual = connection.execute(
                    "SELECT count(*) FROM availability_snapshot"
                ).fetchone()[0]
            if len(rows) != expected or actual != expected:
                raise RuntimeError("Benchmark failed a completeness/idempotency acceptance check")
            measurements.append(
                {
                    "normalize_seconds": collected - before,
                    "initial_write_seconds": inserted - collected,
                    "identical_replay_seconds": replayed - inserted,
                    "total_seconds": replayed - before,
                    "peak_python_mib": peak / 1024**2,
                }
            )
    return {
        "hotels": hotel_count,
        "rows": expected,
        "repetitions": repeats,
        "medians": {
            key: round(statistics.median(run[key] for run in measurements), 6)
            for key in measurements[0]
        },
        "measurements": measurements,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("output/benchmark.json"))
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10:
        parser.error("repeats must be between 1 and 10")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result = {
        "scope": "Synthetic in-process source + local SQLite only; tracemalloc enabled",
        "not_measured": ["real API rate limits", "BigQuery", "Cloud Run", "network latency"],
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cases": [benchmark_case(size, args.repeats, args.output.parent) for size in (2, 20, 80)],
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
