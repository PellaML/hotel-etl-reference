import sqlite3
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from hotel_etl.errors import StorageError, ValidationError
from hotel_etl.models import AvailabilityRow
from hotel_etl.storage.sqlite import SQLiteSink


def row() -> AvailabilityRow:
    return AvailabilityRow(
        "demo",
        "double",
        date(2026, 9, 27),
        7,
        date(2026, 9, 27),
        datetime(2026, 9, 27, 6, tzinfo=UTC),
    )


def read(database: Path) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute(
            "SELECT available_rooms, observed_at FROM availability_snapshot "
            "ORDER BY snapshot_date, stay_date"
        ).fetchall()


def persistent_table_names(database: Path) -> list[str]:
    """Tables stored in the database file; TEMP tables never appear here."""
    with closing(sqlite3.connect(database)) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        return sorted(name for (name,) in rows)


def test_replay_newer_and_out_of_order_observations(tmp_path: Path) -> None:
    path = tmp_path / "test.sqlite"
    sink = SQLiteSink(path)
    original = row()
    sink.write([original])
    sink.write([original])
    assert len(read(path)) == 1
    newer = replace(
        original, observed_at=original.observed_at + timedelta(hours=1), available_rooms=5
    )
    sink.write([newer])
    sink.write([original])
    assert read(path) == [(5, "2026-09-27T07:00:00.000000Z")]
    following_day = replace(
        original,
        observed_at=original.observed_at + timedelta(days=1),
        snapshot_date=original.snapshot_date + timedelta(days=1),
    )
    sink.write([following_day])
    assert len(read(path)) == 2


def test_conflict_rolls_back_entire_batch(tmp_path: Path) -> None:
    path = tmp_path / "test.sqlite"
    sink = SQLiteSink(path)
    original = row()
    sink.write([original])
    new_key = replace(original, stay_date=original.stay_date + timedelta(days=1))
    with pytest.raises(StorageError, match="Conflicting values"):
        sink.write([new_key, replace(original, available_rooms=8)])
    assert read(path) == [(7, "2026-09-27T06:00:00.000000Z")]


def test_empty_batch_and_duplicate_batch_do_not_create_database(tmp_path: Path) -> None:
    path = tmp_path / "test.sqlite"
    sink = SQLiteSink(path)
    sink.write([])
    assert not path.exists()
    with pytest.raises(ValidationError):
        sink.write([row(), row()])
    assert not path.exists()


def test_one_shot_iterator_is_snapshotted_before_validation_and_insert(tmp_path: Path) -> None:
    path = tmp_path / "test.sqlite"
    # Deliberately outside the Sequence contract: validation must not exhaust the rows.
    rows = cast(Sequence[AvailabilityRow], iter([row()]))
    SQLiteSink(path).write(rows)
    assert read(path) == [(7, "2026-09-27T06:00:00.000000Z")]


def test_existing_incompatible_table_is_not_replaced(tmp_path: Path) -> None:
    path = tmp_path / "test.sqlite"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE availability_snapshot (value TEXT)")
        connection.execute("INSERT INTO availability_snapshot VALUES ('keep')")
    with pytest.raises(StorageError, match="incompatible schema"):
        SQLiteSink(path).write([row()])
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT * FROM availability_snapshot").fetchall() == [("keep",)]


def test_missing_parent_directory_fails_safely(tmp_path: Path) -> None:
    with pytest.raises(StorageError, match="SQLite write failed; no changes were committed"):
        SQLiteSink(tmp_path / "missing" / "test.sqlite").write([row()])


def test_concurrent_sqlite_writes_preserve_latest_observation(tmp_path: Path) -> None:
    path = tmp_path / "test.sqlite"
    original = row()
    batches = [
        [
            replace(
                original, observed_at=original.observed_at + timedelta(minutes=i), available_rooms=i
            )
        ]
        for i in range(12)
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(SQLiteSink(path).write, reversed(batches)))
    assert read(path) == [(11, "2026-09-27T06:11:00.000000Z")]
    # Concurrent writers leave no extra persistent table behind.
    assert persistent_table_names(path) == ["availability_snapshot"]


_KEY_NAMES = ("hotel_id", "room_type_id", "snapshot_date", "stay_date")


def create_existing_table(
    path: Path,
    *,
    primary_key: str,
    hotel_collation: str = "BINARY",
    without_rowid: bool = False,
    seed: AvailabilityRow | None = None,
) -> None:
    # All interpolated SQL here is test-owned DDL, never application input.
    suffix = " WITHOUT ROWID" if without_rowid else ""
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            f"""
            CREATE TABLE availability_snapshot (
                hotel_id TEXT COLLATE {hotel_collation} NOT NULL,
                room_type_id TEXT NOT NULL,
                snapshot_date TEXT NOT NULL,
                stay_date TEXT NOT NULL,
                available_rooms INTEGER NOT NULL CHECK (available_rooms >= 0),
                observed_at TEXT NOT NULL,
                PRIMARY KEY ({primary_key})
            ){suffix}
            """
        )
        if seed is not None:
            connection.execute(
                "INSERT INTO availability_snapshot VALUES (?, ?, ?, ?, ?, ?)",
                (
                    seed.hotel_id,
                    seed.room_type_id,
                    seed.snapshot_date.isoformat(),
                    seed.stay_date.isoformat(),
                    seed.available_rooms,
                    seed.to_dict()["observed_at"],
                ),
            )


@pytest.mark.parametrize("key_name", _KEY_NAMES)
@pytest.mark.parametrize("collation", ["NOCASE", "RTRIM"])
def test_nonbinary_primary_key_is_rejected_without_changing_existing_data(
    tmp_path: Path,
    key_name: str,
    collation: str,
) -> None:
    path = tmp_path / "existing.sqlite"
    key = ", ".join(
        f"{name} COLLATE {collation if name == key_name else 'BINARY'}" for name in _KEY_NAMES
    )
    original = row()
    create_existing_table(path, primary_key=key, seed=original)
    before = path.read_bytes()
    with pytest.raises(StorageError, match="BINARY collations"):
        SQLiteSink(path).write([original, replace(original, hotel_id="Demo", available_rooms=9)])
    assert path.read_bytes() == before
    assert read(path) == [(7, "2026-09-27T06:00:00.000000Z")]
    # The refused write added no persistent table.
    assert persistent_table_names(path) == ["availability_snapshot"]


def test_inherited_nocase_primary_key_is_rejected_before_silent_key_collapse(
    tmp_path: Path,
) -> None:
    path = tmp_path / "inherited.sqlite"
    create_existing_table(
        path, primary_key=", ".join(_KEY_NAMES), hotel_collation="NOCASE", seed=row()
    )
    before = path.read_bytes()
    records = [
        replace(row(), hotel_id="Hotel"),
        replace(row(), hotel_id="hotel", available_rooms=9),
    ]
    with pytest.raises(StorageError, match="BINARY collations"):
        SQLiteSink(path).write(records)
    assert path.read_bytes() == before
    assert read(path) == [(7, "2026-09-27T06:00:00.000000Z")]


@pytest.mark.parametrize("descending", [False, True])
@pytest.mark.parametrize("without_rowid", [False, True])
@pytest.mark.parametrize("collation", ["BINARY", "binary"])
def test_binary_primary_key_supports_descending_order_and_serial_replay(
    tmp_path: Path,
    descending: bool,
    without_rowid: bool,
    collation: str,
) -> None:
    path = tmp_path / "binary.sqlite"
    direction = "DESC" if descending else "ASC"
    key = ", ".join(f"{name} COLLATE {collation} {direction}" for name in _KEY_NAMES)
    create_existing_table(path, primary_key=key, without_rowid=without_rowid)
    upper = replace(row(), hotel_id="Hotel", available_rooms=4)
    lower = replace(row(), hotel_id="hotel", available_rooms=9)
    newer = replace(upper, observed_at=upper.observed_at + timedelta(minutes=1), available_rooms=3)
    sink = SQLiteSink(path)
    sink.write([upper, lower])
    sink.write([upper, lower])
    sink.write([newer])
    sink.write([upper, lower])
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute(
            "SELECT hotel_id, available_rooms FROM availability_snapshot "
            "ORDER BY hotel_id COLLATE BINARY"
        ).fetchall() == [("Hotel", 3), ("hotel", 9)]
    with pytest.raises(StorageError, match="Conflicting values"):
        sink.write([replace(newer, available_rooms=6)])


def test_binary_pk_override_uses_binary_identity_even_with_nocase_column(tmp_path: Path) -> None:
    path = tmp_path / "override.sqlite"
    key = ", ".join(f"{name} COLLATE BINARY" for name in _KEY_NAMES)
    create_existing_table(path, primary_key=key, hotel_collation="NOCASE")
    records = [
        replace(row(), hotel_id="Hotel"),
        replace(row(), hotel_id="hotel", available_rooms=9),
    ]
    sink = SQLiteSink(path)
    sink.write(records)
    sink.write(records)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute(
            "SELECT hotel_id, available_rooms FROM availability_snapshot "
            "ORDER BY hotel_id COLLATE BINARY"
        ).fetchall() == [("Hotel", 7), ("hotel", 9)]


def test_extra_nocase_unique_index_cannot_silently_replace_binary_conflict_target(
    tmp_path: Path,
) -> None:
    path = tmp_path / "extra-index.sqlite"
    create_existing_table(path, primary_key=", ".join(_KEY_NAMES))
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "CREATE UNIQUE INDEX restrictive_key ON availability_snapshot "
            "(hotel_id COLLATE NOCASE, room_type_id, snapshot_date, stay_date)"
        )
    before = path.read_bytes()
    records = [
        replace(row(), hotel_id="Hotel"),
        replace(row(), hotel_id="hotel", available_rooms=9),
    ]
    with pytest.raises(StorageError, match="no changes were committed"):
        SQLiteSink(path).write(records)
    assert path.read_bytes() == before
    assert read(path) == []


def test_sqlite_write_does_not_build_export_dictionaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_export(_row: AvailabilityRow) -> dict[str, object]:
        raise AssertionError("SQLite storage should not construct JSON export records")

    monkeypatch.setattr(AvailabilityRow, "to_dict", fail_export)
    path = tmp_path / "test.sqlite"
    SQLiteSink(path).write([row()])
    assert read(path) == [(7, "2026-09-27T06:00:00.000000Z")]
