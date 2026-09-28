"""Parse commands, dispatch jobs, and format top-level results."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from hotel_etl import config
from hotel_etl.api import HttpAvailabilityClient
from hotel_etl.errors import PipelineError, ValidationError
from hotel_etl.models import parse_date, utc_timestamp
from hotel_etl.pipeline import SnapshotSink, sync_snapshot
from hotel_etl.storage.sqlite import SQLiteSink


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import validated daily hotel availability snapshots.",
        epilog="Start without credentials: python -m hotel_etl demo --output-dir output/demo",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Run the synthetic loopback HTTP -> SQLite demo")
    demo.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/demo"),
        help="Fresh output directory for SQLite, NDJSON and summary files (default: output/demo)",
    )
    demo.add_argument(
        "--days", type=int, default=365, help="Stay-date horizon, 1 to 366 days (default: 365)"
    )
    demo.add_argument(
        "--snapshot-date",
        default="2026-09-27",
        help="First synthetic observation and stay date, YYYY-MM-DD (default: 2026-09-27)",
    )
    sync = commands.add_parser(
        "sync", help="Read an explicitly configured API and write a snapshot"
    )
    sync.add_argument(
        "--config", type=Path, required=True, help="JSON file with expected hotel and room-type IDs"
    )
    sync.add_argument(
        "--start-date", help="First stay date, YYYY-MM-DD; defaults to the observation's UTC date"
    )
    sync.add_argument(
        "--snapshot-at", help="Timezone-aware ISO 8601 observation time; default now UTC"
    )
    sync.add_argument(
        "--days", type=int, default=365, help="Stay-date horizon, 1 to 366 days (default: 365)"
    )
    sync.add_argument(
        "--location", default="EU", help="Existing BigQuery dataset location (default: EU)"
    )
    destination = sync.add_mutually_exclusive_group(required=True)
    destination.add_argument(
        "--sqlite", type=Path, help="SQLite destination file; its parent directory must exist"
    )
    destination.add_argument(
        "--bigquery", help="Existing project.dataset.table (writes may incur cost)"
    )
    return parser


def _run_sync(args: argparse.Namespace) -> dict[str, object]:
    base_url, token = (
        os.environ.get("HOTEL_API_BASE_URL"),
        os.environ.get("HOTEL_API_TOKEN"),
    )
    if not base_url or not token:
        raise ValidationError("Set HOTEL_API_BASE_URL and HOTEL_API_TOKEN in the environment.")
    if args.snapshot_at:
        try:
            stamp = utc_timestamp(datetime.fromisoformat(args.snapshot_at))
        except ValueError:
            raise ValidationError("snapshot-at must be a timezone-aware ISO timestamp.") from None
    else:
        stamp = datetime.now(UTC)
    start = parse_date(args.start_date) if args.start_date else stamp.date()
    hotels = config.load_hotels(args.config)
    sink: SnapshotSink
    if args.sqlite:
        sink = SQLiteSink(args.sqlite)
    else:
        from hotel_etl.storage.bigquery import BigQuerySink

        sink = BigQuerySink(args.bigquery, location=args.location)
    rows = sync_snapshot(
        HttpAvailabilityClient(base_url, token),
        sink,
        hotels,
        start_date=start,
        horizon_days=args.days,
        snapshot_at=stamp,
    )
    return {
        "status": "ok",
        "rows_processed": len(rows),
        "snapshot_date": stamp.date().isoformat(),
        "sink": "sqlite" if args.sqlite else "bigquery",
    }


def _error_message(exc: PipelineError | OSError) -> str:
    if not isinstance(exc, PipelineError):
        return "Local file operation failed."
    # This package writes PipelineError messages and notes to be safe to show.
    # Causes can hold SDK or transport details, so they are never printed.
    notes = [note for note in getattr(exc, "__notes__", ()) if isinstance(note, str)]
    return " ".join([str(exc), *notes])


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "demo":
            from hotel_etl import demo

            summary = demo.run_demo(
                args.output_dir, days=args.days, first_date=parse_date(args.snapshot_date)
            )
        else:
            summary = _run_sync(args)
        print(json.dumps(summary, sort_keys=True, allow_nan=False))
        return 0
    except KeyboardInterrupt:
        print(
            json.dumps(
                {"status": "error", "error": "Interrupted. Check the destination before retrying."}
            ),
            file=sys.stderr,
        )
        return 130
    except (PipelineError, OSError) as exc:
        print(json.dumps({"status": "error", "error": _error_message(exc)}), file=sys.stderr)
        return 1
