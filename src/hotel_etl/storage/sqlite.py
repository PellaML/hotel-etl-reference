"""Transactional local sink with the same serial-replay semantics as the cloud adapter."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

from hotel_etl.errors import StorageError
from hotel_etl.models import AvailabilityRow, validate_batch

_COLUMNS = (
    ("hotel_id", "TEXT", 1),
    ("room_type_id", "TEXT", 2),
    ("snapshot_date", "TEXT", 3),
    ("stay_date", "TEXT", 4),
    ("available_rooms", "INTEGER", 0),
    ("observed_at", "TEXT", 0),
)
_DEFINITION = """
    hotel_id TEXT NOT NULL,
    room_type_id TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    stay_date TEXT NOT NULL,
    available_rooms INTEGER NOT NULL CHECK (available_rooms >= 0),
    observed_at TEXT NOT NULL,
    PRIMARY KEY (hotel_id, room_type_id, snapshot_date, stay_date)
"""


class SQLiteSink:
    def __init__(self, database: str | Path) -> None:
        self.database = Path(database)

    def write(self, rows: Sequence[AvailabilityRow]) -> None:
        # Validation and the insert both iterate the rows, so snapshot them once.
        batch = tuple(rows)
        validate_batch(batch)
        if not batch:
            return
        try:
            with (
                closing(
                    sqlite3.connect(self.database, timeout=30, isolation_level=None)
                ) as connection,
                connection,
            ):
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    f"CREATE TABLE IF NOT EXISTS availability_snapshot ({_DEFINITION})"
                )
                schema = connection.execute("PRAGMA table_info(availability_snapshot)").fetchall()
                actual = tuple((column[1], column[2], column[5]) for column in schema)
                if actual != _COLUMNS or any(column[3] != 1 for column in schema):
                    raise StorageError(
                        "Existing SQLite table has an incompatible schema; left unchanged."
                    )
                key_schema = connection.execute(
                    """
                    SELECT parts.name, parts.coll
                    FROM pragma_index_list('availability_snapshot') AS indexes
                    JOIN pragma_index_xinfo(indexes.name) AS parts
                    WHERE indexes.origin = 'pk' AND parts."key" = 1
                    ORDER BY parts.seqno
                    """
                ).fetchall()
                expected_key = tuple((name, "BINARY") for name, _, order in _COLUMNS if order)
                actual_key = tuple((name, str(collation).upper()) for name, collation in key_schema)
                # ASC/DESC changes ordering, not identity. Ignore auxiliary
                # rowid/non-key entries, including those in WITHOUT ROWID tables.
                if actual_key != expected_key:
                    raise StorageError(
                        "Existing SQLite primary key must use BINARY collations; left unchanged."
                    )
                connection.execute(f"CREATE TEMP TABLE incoming ({_DEFINITION})")
                connection.executemany(
                    "INSERT INTO incoming VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        (
                            row.hotel_id,
                            row.room_type_id,
                            row.snapshot_date.isoformat(),
                            row.stay_date.isoformat(),
                            row.available_rooms,
                            row.observed_at_text,
                        )
                        for row in batch
                    ),
                )
                # A column can have a different default collation than its PK
                # override. Both conflict detection and UPSERT must use key identity.
                conflict = connection.execute(
                    """
                    SELECT 1 FROM availability_snapshot AS target JOIN incoming AS source
                      ON target.hotel_id COLLATE BINARY = source.hotel_id
                     AND target.room_type_id COLLATE BINARY = source.room_type_id
                     AND target.snapshot_date COLLATE BINARY = source.snapshot_date
                     AND target.stay_date COLLATE BINARY = source.stay_date
                    WHERE target.observed_at = source.observed_at
                      AND target.available_rooms != source.available_rooms LIMIT 1
                    """
                ).fetchone()
                if conflict is not None:
                    raise StorageError(
                        "Conflicting values for an existing observation; no rows written."
                    )
                # SQLite needs a WHERE clause, even WHERE 1, to parse
                # INSERT ... SELECT ... ON CONFLICT without ambiguity.
                connection.execute(
                    """
                    INSERT INTO availability_snapshot SELECT * FROM incoming WHERE 1
                    ON CONFLICT (
                        hotel_id COLLATE BINARY, room_type_id COLLATE BINARY,
                        snapshot_date COLLATE BINARY, stay_date COLLATE BINARY
                    )
                    DO UPDATE SET available_rooms = excluded.available_rooms,
                                  observed_at = excluded.observed_at
                    WHERE excluded.observed_at > availability_snapshot.observed_at
                    """
                )
        except sqlite3.Error:
            raise StorageError("SQLite write failed; no changes were committed.") from None
