"""Network-free SDK contract tests, not a BigQuery SQL execution emulator."""

from __future__ import annotations

import builtins
import importlib
import json
import socket
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta, tzinfo
from itertools import count
from pathlib import Path
from sys import float_info
from types import ModuleType, SimpleNamespace
from typing import Any, NoReturn, cast
from urllib.parse import urlsplit
from uuid import UUID

import pytest

from hotel_etl import cli
from hotel_etl.errors import StorageError, ValidationError
from hotel_etl.models import AvailabilityRow
from hotel_etl.storage import bigquery as sink_module

try:
    import google.auth
    import requests
    from google.api_core.exceptions import BadRequest, Conflict, GoogleAPICallError, NotFound
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import bigquery
except ImportError:
    pytest.skip("optional BigQuery SDK is not installed", allow_module_level=True)

# Saved before the autouse fixture below replaces bigquery.Client in every test.
_SDK_CLIENT = bigquery.Client
TABLE_ID = "example-project.analytics.availability"
RUN_ID = "00000000000000000000000000000001"
STAGING_ID = f"example-project.analytics._hotel_etl_stage_{RUN_ID}"
LOAD_JOB_ID = f"hotel_etl_load_{RUN_ID}"
MERGE_JOB_PREFIX = f"hotel_etl_merge_{RUN_ID}_"
# The fake client appends this where the SDK would append a random UUID.
MERGE_JOB_ID = f"{MERGE_JOB_PREFIX}synthetic"
REQUEST_TIMEOUT = 60.0
NOW = datetime(2026, 9, 27, 12, tzinfo=UTC)
STAGING_EXPIRY_TEXT = "2026-09-27T13:00:00Z"
COLUMNS = (
    ("hotel_id", "STRING"),
    ("room_type_id", "STRING"),
    ("stay_date", "DATE"),
    ("available_rooms", "INTEGER"),
    ("snapshot_date", "DATE"),
    ("observed_at", "TIMESTAMP"),
)


def _schema() -> list[bigquery.SchemaField]:
    return [bigquery.SchemaField(name, field_type, mode="REQUIRED") for name, field_type in COLUMNS]


def _destination(**overrides: object) -> bigquery.Table:
    resource: dict[str, object] = {
        "tableReference": {
            "projectId": "example-project",
            "datasetId": "analytics",
            "tableId": "availability",
        },
        "type": "TABLE",
        "location": "EU",
        "schema": {"fields": [field.to_api_repr() for field in _schema()]},
        "timePartitioning": {"type": "DAY", "field": "snapshot_date"},
        "requirePartitionFilter": True,
    }
    resource.update(overrides)
    return bigquery.Table.from_api_repr(resource)


def _row() -> AvailabilityRow:
    return AvailabilityRow(
        hotel_id="synthetic_hotel",
        room_type_id="double",
        stay_date=date(2026, 10, 1),
        available_rooms=4,
        snapshot_date=NOW.date(),
        observed_at=NOW,
    )


@dataclass
class _Load:
    rows: list[dict[str, object]]
    destination: str
    config: bigquery.LoadJobConfig
    job_id: str
    location: str


@dataclass
class _Query:
    sql: str
    config: bigquery.QueryJobConfig
    job_id_prefix: str
    location: str
    job_retry: object


class _Job:
    def __init__(self, client: _FakeClient, phase: str, job_id: str) -> None:
        self.client = client
        self.phase = phase
        self.job_id = job_id

    def result(self, timeout: float | None = None, **options: object) -> None:
        self.client.waits.append((self.phase, timeout))
        self.client.result_options.append((self.phase, options))
        self.client.step(f"{self.phase}.result")


class _FakeClient:
    """Record requests and inject failures; deliberately do not pretend to run SQL."""

    def __init__(self, destination: bigquery.Table | None = None) -> None:
        self.destination = destination if destination is not None else _destination()
        self.events: list[str] = []
        self.failures: dict[str, BaseException] = {}
        self.lookups: list[str] = []
        self.creates: list[tuple[bigquery.Table, bool]] = []
        self.loads: list[_Load] = []
        self.queries: list[_Query] = []
        self.waits: list[tuple[str, float | None]] = []
        self.result_options: list[tuple[str, dict[str, object]]] = []
        self.deletes: list[tuple[str, bool]] = []
        self.request_timeouts: list[tuple[str, float]] = []
        self.live_tables = {str(self.destination.reference)}

    def step(self, phase: str) -> None:
        self.events.append(phase)
        if phase in self.failures:
            raise self.failures[phase]

    def get_table(self, table_id: str, *, timeout: float) -> bigquery.Table:
        self.lookups.append(table_id)
        self.request_timeouts.append(("get_table", timeout))
        self.step("get_table")
        return self.destination

    def create_table(
        self, table: bigquery.Table, *, exists_ok: bool, timeout: float
    ) -> bigquery.Table:
        self.creates.append((table, exists_ok))
        self.request_timeouts.append(("create_table", timeout))
        self.step("create_table")
        table_id = str(table.reference)
        if table_id in self.live_tables:
            raise Conflict("Synthetic staging-name collision")  # type: ignore[no-untyped-call]
        self.live_tables.add(table_id)
        return table

    def load_table_from_json(
        self,
        rows: list[dict[str, object]],
        destination: str,
        *,
        job_config: bigquery.LoadJobConfig,
        job_id: str,
        location: str,
    ) -> _Job:
        self.loads.append(_Load(rows, destination, job_config, job_id, location))
        self.step("load")
        return _Job(self, "load", job_id)

    # Keyword-only parameters make the fake reject a fixed job_id for the MERGE.
    def query(
        self,
        sql: str,
        *,
        job_config: bigquery.QueryJobConfig,
        job_id_prefix: str,
        location: str,
        timeout: float,
        job_retry: object,
    ) -> _Job:
        self.queries.append(_Query(sql, job_config, job_id_prefix, location, job_retry))
        self.request_timeouts.append(("query", timeout))
        self.step("query")
        return _Job(self, "query", f"{job_id_prefix}synthetic")

    def delete_table(self, table_id: str, *, not_found_ok: bool, timeout: float) -> None:
        self.deletes.append((table_id, not_found_ok))
        self.request_timeouts.append(("delete_table", timeout))
        self.step("delete_table")
        self.live_tables.discard(table_id)


class _Clock:
    @staticmethod
    def now(tz: tzinfo | None = None) -> datetime:
        assert tz is UTC
        return NOW


def _forbid_external_access(*args: object, **kwargs: object) -> NoReturn:
    raise AssertionError("Tests must not use sockets, credentials, or a real BigQuery client")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    identifiers = count(1)
    monkeypatch.setattr(sink_module, "uuid4", lambda: UUID(int=next(identifiers)))
    monkeypatch.setattr(sink_module, "datetime", _Clock)
    monkeypatch.setattr(socket.socket, "connect", _forbid_external_access)
    monkeypatch.setattr(socket.socket, "connect_ex", _forbid_external_access)
    monkeypatch.setattr(google.auth, "default", _forbid_external_access)
    monkeypatch.setattr(bigquery, "Client", _forbid_external_access)


@pytest.mark.parametrize(
    "table_id",
    [
        "",
        "analytics.availability",
        "example-project.analytics.availability.extra",
        "`example-project.analytics.availability`",
        "example-project.analytics.availability; DROP TABLE victims",
        "example-project.analytics.availability--comment",
        "example-project.analytics.availability/*comment*/",
        "example-project.analytics.availability$20260927",
        "example-project.analytics.availability@123456",
        "example-project.analytics.*",
        "example-project.analytics.availability\n",
        " example-project.analytics.availability",
        "example-project.analytics.availability ",
        "example-project.analytics.a`b",
        "example-project.analytics.a-b",
        "example-project.analytics.1table",
        "example-project.analytics.é",
        "example-project.an-alytics.availability",
        "domain:example-project.analytics.availability",
        "EXAMPLE-project.analytics.availability",
        "example-project-.analytics.availability",
        "123456.analytics.availability",
        "short.analytics.availability",
        f"{'a' * 31}.analytics.availability",
        f"example-project.{'a' * 1025}.availability",
        f"example-project.analytics.{'a' * 1025}",
    ],
)
def test_rejects_unsafe_or_unsupported_identifiers_before_io(table_id: str) -> None:
    client = _FakeClient()
    with pytest.raises(ValidationError, match="table_id"):
        sink_module.BigQuerySink(table_id, client=client)
    assert client.events == []


@pytest.mark.parametrize("value", [None, 12, [], {}])
def test_rejects_non_string_identifier(value: object) -> None:
    with pytest.raises(ValidationError, match="table_id"):
        sink_module.BigQuerySink(cast(str, value), client=_FakeClient())


@pytest.mark.parametrize(
    "table_id",
    [
        TABLE_ID,
        "a12345._dataset._table_123",
        f"{'a' * 30}.{'D' * 1024}.{'T' * 1024}",
    ],
)
def test_accepts_supported_identifier_boundaries_without_io(table_id: str) -> None:
    client = _FakeClient()
    sink_module.BigQuerySink(table_id, client=client)
    assert client.events == []


@pytest.mark.parametrize("location", ["", " EU", "EU\n", "eu;drop", "eu/other", None, 1])
def test_rejects_invalid_location(location: object) -> None:
    with pytest.raises(ValidationError, match="location"):
        sink_module.BigQuerySink(TABLE_ID, location=cast(str, location), client=_FakeClient())


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        None,
        "300",
        0,
        -1,
        0.0,
        -0.0,
        -0.5,
        float("nan"),
        float("inf"),
        float("-inf"),
        10**1000,
        [],
        {},
    ],
    ids=[
        "true",
        "false",
        "none",
        "string",
        "zero-int",
        "negative-int",
        "zero-float",
        "negative-zero",
        "negative-float",
        "nan",
        "infinity",
        "negative-infinity",
        "oversized-int",
        "list",
        "dict",
    ],
)
def test_rejects_invalid_job_wait_timeout_before_io(value: object) -> None:
    client = _FakeClient()
    with pytest.raises(ValidationError, match="job_wait_timeout"):
        sink_module.BigQuerySink(TABLE_ID, client=client, job_wait_timeout=cast(float, value))
    assert client.events == []
    assert client.waits == []


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        None,
        "1073741824",
        0,
        -1,
        1.0,
        1.5,
        float("nan"),
        float("inf"),
        float("-inf"),
        2**63,
        [],
        {},
    ],
    ids=[
        "true",
        "false",
        "none",
        "string",
        "zero",
        "negative",
        "integral-float",
        "fraction",
        "nan",
        "infinity",
        "negative-infinity",
        "int64-overflow",
        "list",
        "dict",
    ],
)
def test_rejects_invalid_maximum_bytes_billed_before_io(value: object) -> None:
    client = _FakeClient()
    with pytest.raises(ValidationError, match="maximum_bytes_billed"):
        sink_module.BigQuerySink(TABLE_ID, client=client, maximum_bytes_billed=cast(int, value))
    assert client.events == []
    assert client.waits == []


@pytest.mark.parametrize(
    ("timeout", "byte_limit"),
    [(12.5, 2 * 1024**3), (1, 1), (0.125, 2**63 - 1)],
)
def test_custom_job_limits_are_lazy_and_reach_both_waits_and_sdk_config(
    timeout: float,
    byte_limit: int,
) -> None:
    client = _FakeClient()
    sink = sink_module.BigQuerySink(
        TABLE_ID, client=client, job_wait_timeout=timeout, maximum_bytes_billed=byte_limit
    )
    sink.write([])
    assert client.events == []
    assert client.waits == []
    sink.write([_row()])
    assert client.waits == [("load", float(timeout)), ("query", float(timeout))]
    config = client.queries[0].config
    assert isinstance(config, bigquery.QueryJobConfig)
    assert config.maximum_bytes_billed == byte_limit
    assert config.to_api_repr()["query"]["maximumBytesBilled"] == str(byte_limit)


def test_empty_batch_is_noop_with_or_without_client() -> None:
    client = _FakeClient()
    sink_module.BigQuerySink(TABLE_ID, client=client).write([])
    sink_module.BigQuerySink(TABLE_ID).write(())
    assert client.events == []


def test_module_and_empty_write_work_when_sdk_import_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__

    def without_google(
        name: str,
        globals: Mapping[str, object] | None = None,
        locals: Mapping[str, object] | None = None,
        fromlist: Sequence[str] = (),
        level: int = 0,
    ) -> ModuleType:
        if name == "google" or name.startswith("google."):
            raise ModuleNotFoundError("Synthetic missing optional SDK")
        return original_import(name, globals, locals, fromlist, level)

    client = _FakeClient()
    monkeypatch.setattr(builtins, "__import__", without_google)
    reloaded = importlib.reload(sink_module)
    sink = reloaded.BigQuerySink(
        TABLE_ID, client=client, job_wait_timeout=5, maximum_bytes_billed=4096
    )
    sink.write([])
    with pytest.raises(StorageError, match="optional google-cloud-bigquery"):
        sink.write([_row()])
    assert client.events == []


@pytest.mark.parametrize("variant", ["identical", "different_value", "newer_time"])
def test_rejects_all_duplicate_batch_keys_before_io(variant: str) -> None:
    row = _row()
    other = {
        "identical": row,
        "different_value": replace(row, available_rooms=5),
        "newer_time": replace(row, observed_at=NOW + timedelta(minutes=1)),
    }[variant]
    client = _FakeClient()
    with pytest.raises(ValidationError, match="(?i)duplicate"):
        sink_module.BigQuerySink(TABLE_ID, client=client).write([row, other])
    assert client.events == []


def test_non_record_batch_is_rejected_by_shared_validation_before_io() -> None:
    client = _FakeClient()
    invalid = cast(Sequence[AvailabilityRow], [_row(), {"hotel_id": "not_a_validated_record"}])
    with pytest.raises(ValidationError, match="AvailabilityRow"):
        sink_module.BigQuerySink(TABLE_ID, client=client).write(invalid)
    assert client.events == []


@pytest.mark.parametrize("key_field", ["hotel_id", "room_type_id", "snapshot_date", "stay_date"])
def test_every_key_component_distinguishes_rows(key_field: str) -> None:
    row = _row()
    variants = {
        "hotel_id": replace(row, hotel_id="other_hotel"),
        "room_type_id": replace(row, room_type_id="single"),
        "snapshot_date": replace(
            row, snapshot_date=date(2026, 9, 26), observed_at=NOW - timedelta(days=1)
        ),
        "stay_date": replace(row, stay_date=date(2026, 10, 2)),
    }
    client = _FakeClient()
    sink_module.BigQuerySink(TABLE_ID, client=client).write([row, variants[key_field]])
    assert len(client.loads[0].rows) == 2


@pytest.mark.parametrize(
    "schema",
    [
        _schema()[:-1],
        _schema() + [bigquery.SchemaField("extra", "STRING", mode="REQUIRED")],
        _schema()[:-1] + [bigquery.SchemaField("observed_at", "DATE", mode="REQUIRED")],
        [bigquery.SchemaField("hotel_id", "STRING", mode="NULLABLE"), *_schema()[1:]],
        [bigquery.SchemaField("hotel_id", "STRING", mode="REPEATED"), *_schema()[1:]],
        [_schema()[1], *_schema()[1:]],
        [
            bigquery.SchemaField(
                "hotel_id",
                "STRING",
                mode="REQUIRED",
                fields=[bigquery.SchemaField("child", "STRING")],
            ),
            *_schema()[1:],
        ],
        [bigquery.SchemaField("hotel_id", "STRING", mode="REQUIRED", max_length=5), *_schema()[1:]],
        [
            bigquery.SchemaField(
                "hotel_id", "STRING", mode="REQUIRED", default_value_expression="'x'"
            ),
            *_schema()[1:],
        ],
        [
            bigquery.SchemaField.from_api_repr(
                {"name": "hotel_id", "type": "STRING", "mode": "REQUIRED", "collation": "und:ci"}
            ),
            *_schema()[1:],
        ],
    ],
    ids=[
        "missing",
        "extra",
        "type",
        "nullable",
        "repeated",
        "duplicate",
        "nested",
        "length",
        "default",
        "collation",
    ],
)
def test_schema_preflight_is_exact_and_never_mutates_target(
    schema: list[bigquery.SchemaField],
) -> None:
    table = _destination()
    table.schema = schema
    client = _FakeClient(table)
    with pytest.raises(StorageError, match="six REQUIRED"):
        sink_module.BigQuerySink(TABLE_ID, client=client).write([_row()])
    assert client.events == ["get_table"]
    assert client.live_tables == {TABLE_ID}


def test_rejects_default_case_insensitive_collation() -> None:
    client = _FakeClient(_destination(defaultCollation="und:ci"))
    with pytest.raises(StorageError, match="collation"):
        sink_module.BigQuerySink(TABLE_ID, client=client).write([_row()])
    assert client.events == ["get_table"]


def test_schema_field_order_descriptions_and_int64_alias_are_not_semantic_changes() -> None:
    table = _destination()
    table.schema = list(
        reversed(
            [
                bigquery.SchemaField(
                    name,
                    "INT64" if name == "available_rooms" else field_type,
                    mode="REQUIRED",
                    description="Synthetic column documentation",
                )
                for name, field_type in COLUMNS
            ]
        )
    )
    client = _FakeClient(table)
    sink_module.BigQuerySink(TABLE_ID, client=client).write([_row()])
    assert len(client.queries) == 1


@pytest.mark.parametrize("table_type", ["VIEW", "MATERIALIZED_VIEW", "EXTERNAL", "SNAPSHOT", None])
def test_rejects_nonordinary_destinations(table_type: str | None) -> None:
    client = _FakeClient(_destination(type=table_type))
    with pytest.raises(StorageError, match="ordinary table"):
        sink_module.BigQuerySink(TABLE_ID, client=client).write([_row()])
    assert client.events == ["get_table"]


@pytest.mark.parametrize("location", ["US", "europe-west1", "", None])
def test_rejects_mismatched_or_missing_destination_location(location: str | None) -> None:
    client = _FakeClient(_destination(location=location))
    with pytest.raises(StorageError, match="location"):
        sink_module.BigQuerySink(TABLE_ID, client=client).write([_row()])
    assert client.events == ["get_table"]


def test_location_matching_is_case_insensitive() -> None:
    client = _FakeClient()
    sink_module.BigQuerySink(TABLE_ID, location="eu", client=client).write([_row()])
    assert client.loads[0].location == "eu"
    assert client.queries[0].location == "eu"


def test_rejects_wrong_table_identity_from_metadata() -> None:
    client = _FakeClient(
        _destination(
            tableReference={
                "projectId": "another-project",
                "datasetId": "analytics",
                "tableId": "availability",
            }
        )
    )
    with pytest.raises(StorageError, match="metadata"):
        sink_module.BigQuerySink(TABLE_ID, client=client).write([_row()])
    assert client.events == ["get_table"]


@pytest.mark.parametrize(
    "partitioning",
    [
        None,
        {"type": "DAY"},
        {"type": "DAY", "field": "stay_date"},
        {"type": "MONTH", "field": "snapshot_date"},
        {"type": "YEAR", "field": "snapshot_date"},
        {"type": "HOUR", "field": "snapshot_date"},
    ],
    ids=["unpartitioned", "ingestion", "wrong_field", "monthly", "yearly", "hourly"],
)
def test_rejects_wrong_partitioning(partitioning: dict[str, str] | None) -> None:
    client = _FakeClient(_destination(timePartitioning=partitioning))
    with pytest.raises(StorageError, match="DAY-partitioned on snapshot_date"):
        sink_module.BigQuerySink(TABLE_ID, client=client).write([_row()])
    assert client.events == ["get_table"]


def test_rejects_range_partitioning() -> None:
    client = _FakeClient(
        _destination(
            rangePartitioning={
                "field": "available_rooms",
                "range": {"start": "0", "end": "100", "interval": "1"},
            }
        )
    )
    with pytest.raises(StorageError, match="DAY-partitioned"):
        sink_module.BigQuerySink(TABLE_ID, client=client).write([_row()])
    assert client.events == ["get_table"]


@pytest.mark.parametrize("require_filter", [False, True])
def test_partition_filter_is_in_sql_regardless_of_table_setting(require_filter: bool) -> None:
    client = _FakeClient(_destination(requirePartitionFilter=require_filter))
    sink_module.BigQuerySink(TABLE_ID, client=client).write([_row()])
    assert "ON T.snapshot_date IN UNNEST(@snapshot_dates)" in client.queries[0].sql


def test_existing_destination_is_required() -> None:
    client = _FakeClient()
    error = NotFound("Synthetic missing table")  # type: ignore[no-untyped-call]
    client.failures["get_table"] = error
    with pytest.raises(StorageError, match="existing BigQuery destination") as raised:
        sink_module.BigQuerySink(TABLE_ID, client=client).write([_row()])
    assert raised.value.__cause__ is error
    assert client.events == ["get_table"]
    assert client.creates == []
    assert client.deletes == []


def test_success_uses_real_sdk_configs_expiring_staging_and_ordered_job_waits() -> None:
    client = _FakeClient()
    row = replace(_row(), available_rooms=2**63 - 1)
    sink_module.BigQuerySink(TABLE_ID, client=client).write([row])
    assert client.events == [
        "get_table",
        "create_table",
        "load",
        "load.result",
        "query",
        "query.result",
        "delete_table",
    ]
    assert client.lookups == [TABLE_ID]
    assert client.waits == [("load", 300.0), ("query", 300.0)]
    assert client.request_timeouts == [
        ("get_table", REQUEST_TIMEOUT),
        ("create_table", REQUEST_TIMEOUT),
        ("query", REQUEST_TIMEOUT),
        ("delete_table", REQUEST_TIMEOUT),
    ]
    staging, exists_ok = client.creates[0]
    assert isinstance(staging, bigquery.Table)
    assert str(staging.reference) == STAGING_ID
    assert exists_ok is False
    assert staging.expires == NOW + timedelta(hours=1)
    assert staging.schema == _schema()
    load = client.loads[0]
    assert isinstance(load.config, bigquery.LoadJobConfig)
    assert load.rows == [row.to_dict()]
    assert load.rows[0]["available_rooms"] == 2**63 - 1
    assert load.destination == STAGING_ID
    assert load.job_id == LOAD_JOB_ID
    assert load.location == "EU"
    assert load.config.schema == _schema()
    assert load.config.create_disposition == "CREATE_NEVER"
    assert load.config.write_disposition == "WRITE_EMPTY"
    assert load.config.source_format == "NEWLINE_DELIMITED_JSON"
    assert load.config.autodetect is False
    assert load.config.ignore_unknown_values is False
    assert load.config.max_bad_records == 0
    query = client.queries[0]
    assert isinstance(query.config, bigquery.QueryJobConfig)
    assert query.config.use_legacy_sql is False
    assert query.config.dry_run is False
    assert query.config.maximum_bytes_billed == 1024**3
    assert query.config.to_api_repr()["query"]["maximumBytesBilled"] == "1073741824"
    assert query.location == "EU"
    assert query.job_id_prefix == MERGE_JOB_PREFIX
    assert query.config.destination is None
    assert query.config.write_disposition is None
    assert query.job_retry is None
    assert client.result_options == [("load", {}), ("query", {"job_retry": None})]
    assert client.deletes == [(STAGING_ID, True)]
    assert client.live_tables == {TABLE_ID}


def test_sparse_snapshot_dates_are_sorted_deduplicated_bound_date_parameters() -> None:
    rows = [
        _row(),
        replace(_row(), stay_date=date(2026, 10, 2)),
        replace(
            _row(), snapshot_date=date(2026, 9, 1), observed_at=datetime(2026, 9, 1, tzinfo=UTC)
        ),
    ]
    client = _FakeClient()
    sink_module.BigQuerySink(TABLE_ID, client=client).write(rows)
    query = client.queries[0]
    assert len(query.config.query_parameters) == 1
    parameter = query.config.query_parameters[0]
    assert isinstance(parameter, bigquery.ArrayQueryParameter)
    assert parameter.to_api_repr() == {
        "name": "snapshot_dates",
        "parameterType": {"type": "ARRAY", "arrayType": {"type": "DATE"}},
        "parameterValue": {"arrayValues": [{"value": "2026-09-01"}, {"value": "2026-09-27"}]},
    }
    assert "2026-09-01" not in query.sql
    assert "2026-09-27" not in query.sql
    assert rows[0].hotel_id not in query.sql
    assert "BETWEEN" not in query.sql.upper()


def test_sql_has_atomic_conflict_guards_and_strictly_newer_only_updates() -> None:
    client = _FakeClient()
    sink_module.BigQuerySink(TABLE_ID, client=client).write([_row()])
    assert len(client.queries) == 1
    sql = " ".join(client.queries[0].sql.split())
    assert sql.startswith("BEGIN TRANSACTION;")
    assert sql.endswith("COMMIT TRANSACTION;")
    assert sql.count("ASSERT NOT EXISTS (") == 2
    merge_position = sql.index(f"MERGE `{TABLE_ID}` AS T")
    assertions = sql[:merge_position]
    assert "HAVING COUNT(*) > 1" in assertions
    assert "GROUP BY T.hotel_id, T.room_type_id, T.snapshot_date, T.stay_date" in assertions
    assert (
        "AND T.observed_at = S.observed_at AND T.available_rooms != S.available_rooms" in assertions
    )
    assert sql.count("T.snapshot_date IN UNNEST(@snapshot_dates)") == 3
    assert "AND S.snapshot_date IN UNNEST(@snapshot_dates)" in assertions
    merge = sql[merge_position:]
    assert f"FROM `{STAGING_ID}` WHERE snapshot_date IN UNNEST(@snapshot_dates)" in merge
    assert "ON T.snapshot_date IN UNNEST(@snapshot_dates)" in merge
    for field in ("hotel_id", "room_type_id", "snapshot_date", "stay_date"):
        assert f"T.{field} = S.{field}" in merge
        assert f"T.{field} = S.{field}" in assertions
    assert "WHEN MATCHED AND S.observed_at > T.observed_at THEN UPDATE SET" in merge
    assert "UPDATE SET available_rooms = S.available_rooms, observed_at = S.observed_at" in merge
    assert "WHEN NOT MATCHED THEN INSERT" in merge
    assert "BY SOURCE" not in merge
    assert "DELETE" not in merge
    assert "TRUNCATE" not in sql
    assert "CREATE OR REPLACE" not in sql
    assert sql.count(f"`{TABLE_ID}`") == 3
    assert sql.count(f"`{STAGING_ID}`") == 2


@pytest.mark.parametrize("phase", ["create_table", "load", "load.result", "query", "query.result"])
def test_failures_cleanup_only_confirmed_owned_staging(phase: str) -> None:
    client = _FakeClient()
    error = BadRequest(f"Synthetic {phase} failure")  # type: ignore[no-untyped-call]
    client.failures[phase] = error
    with pytest.raises(StorageError, match="failed") as raised:
        sink_module.BigQuerySink(TABLE_ID, client=client).write([_row()])
    assert raised.value.__cause__ is error
    expected_deletes = [] if phase == "create_table" else [(STAGING_ID, True)]
    assert client.deletes == expected_deletes
    assert client.live_tables == {TABLE_ID}
    assert client.creates[0][0].expires == NOW + timedelta(hours=1)
    if phase in {"create_table", "load", "load.result"}:
        assert client.queries == []


@pytest.mark.parametrize("phase", ["create_table", "load", "load.result"])
def test_failures_before_merge_submission_report_an_unchanged_destination(phase: str) -> None:
    client = _FakeClient()
    error = BadRequest(f"Synthetic {phase} failure")  # type: ignore[no-untyped-call]
    client.failures[phase] = error
    with pytest.raises(StorageError) as raised:
        sink_module.BigQuerySink(TABLE_ID, client=client).write([_row()])
    message = str(raised.value)
    operation = "staging creation" if phase == "create_table" else "staging load"
    assert message.startswith(f"BigQuery {operation} failed before the merge was submitted")
    assert "this run did not change the destination" in message
    assert "outcome may be unknown" not in message
    assert (f"Load job: {LOAD_JOB_ID}." in message) is (phase != "create_table")
    assert client.queries == []


@pytest.mark.parametrize(
    ("phase", "job_reference"),
    [
        ("query", f"query jobs whose IDs start with {MERGE_JOB_PREFIX}"),
        ("query.result", f"query job {MERGE_JOB_ID}"),
    ],
)
def test_merge_failures_name_the_job_to_verify(phase: str, job_reference: str) -> None:
    client = _FakeClient()
    error = BadRequest(f"Synthetic {phase} failure")  # type: ignore[no-untyped-call]
    client.failures[phase] = error
    with pytest.raises(StorageError) as raised:
        sink_module.BigQuerySink(TABLE_ID, location="eu", client=client).write([_row()])
    assert str(raised.value) == (
        "BigQuery transactional merge failed; job outcome may be unknown. "
        f"Check {job_reference} in location eu before any replay under the single-writer "
        "rule; this sink does not automatically retry."
    )
    assert client.deletes == [(STAGING_ID, True)]


@pytest.mark.parametrize("phase", ["load.result", "query.result"])
def test_wait_timeouts_clean_up_without_resubmission(phase: str) -> None:
    client = _FakeClient()
    timeout_error = TimeoutError("Synthetic result wait timeout")
    client.failures[phase] = timeout_error
    with pytest.raises(StorageError) as raised:
        sink_module.BigQuerySink(TABLE_ID, client=client, job_wait_timeout=0.5).write([_row()])
    message = str(raised.value)
    assert raised.value.__cause__ is timeout_error
    assert "does not automatically retry" in message
    if phase == "query.result":
        # After submission a wait timeout does not cancel the job, so the commit
        # outcome is unknown until an operator checks the named job.
        assert "outcome may be unknown" in message
        assert "single-writer" in message
        assert MERGE_JOB_ID in message
    else:
        assert "did not change the destination" in message
        assert LOAD_JOB_ID in message
    assert len(client.creates) == 1
    assert len(client.loads) == 1
    assert len(client.queries) == (0 if phase == "load.result" else 1)
    expected_waits = [("load", 0.5)]
    if phase == "query.result":
        expected_waits.append(("query", 0.5))
    assert client.waits == expected_waits
    assert client.deletes == [(STAGING_ID, True)]
    assert client.live_tables == {TABLE_ID}


def test_staging_collision_is_not_reused_loaded_or_deleted() -> None:
    client = _FakeClient()
    client.live_tables.add(STAGING_ID)
    with pytest.raises(StorageError) as raised:
        sink_module.BigQuerySink(TABLE_ID, client=client).write([_row()])
    assert isinstance(raised.value.__cause__, Conflict)
    assert client.events == ["get_table", "create_table"]
    assert client.loads == []
    assert client.deletes == []
    assert client.live_tables == {TABLE_ID, STAGING_ID}


def test_staging_name_equal_to_destination_can_never_replace_or_delete_it() -> None:
    table = _destination(
        tableReference={
            "projectId": "example-project",
            "datasetId": "analytics",
            "tableId": "_hotel_etl_stage_00000000000000000000000000000001",
        }
    )
    client = _FakeClient(table)
    with pytest.raises(StorageError) as raised:
        sink_module.BigQuerySink(STAGING_ID, client=client).write([_row()])
    assert isinstance(raised.value.__cause__, Conflict)
    assert client.loads == []
    assert client.deletes == []
    assert client.live_tables == {STAGING_ID}


@pytest.mark.parametrize("phase", ["load", "load.result", "query", "query.result"])
def test_cleanup_failure_does_not_hide_primary_failure(phase: str) -> None:
    client = _FakeClient()
    primary = BadRequest("Synthetic primary failure")  # type: ignore[no-untyped-call]
    client.failures[phase] = primary
    client.failures["delete_table"] = RuntimeError("Synthetic cleanup failure")
    with pytest.raises(StorageError) as raised:
        sink_module.BigQuerySink(TABLE_ID, client=client).write([_row()])
    assert raised.value.__cause__ is primary
    assert raised.value.__notes__ == [
        f"Staging cleanup also failed; staging table {STAGING_ID} expires at {STAGING_EXPIRY_TEXT}."
    ]
    assert client.deletes == [(STAGING_ID, True)]
    assert client.live_tables == {TABLE_ID, STAGING_ID}


def test_cleanup_failure_after_success_is_not_silently_reported_as_success() -> None:
    client = _FakeClient()
    cleanup_error = RuntimeError("Synthetic cleanup failure")
    client.failures["delete_table"] = cleanup_error
    with pytest.raises(StorageError, match="merge completed, but staging cleanup failed") as raised:
        sink_module.BigQuerySink(TABLE_ID, client=client).write([_row()])
    assert raised.value.__cause__ is cleanup_error
    assert str(raised.value).endswith(f"{STAGING_ID} expires at {STAGING_EXPIRY_TEXT}.")
    assert "query.result" in client.events
    assert client.live_tables == {TABLE_ID, STAGING_ID}


@pytest.mark.parametrize(
    ("wait", "lifetime"),
    [
        (0.125, timedelta(hours=1)),
        (300, timedelta(hours=1)),
        (900, timedelta(hours=1)),
        (1800, timedelta(minutes=90)),
        (86_400, timedelta(hours=48, minutes=30)),
        (float_info.max, timedelta(hours=48, minutes=30)),
    ],
    ids=["fraction", "default", "boundary", "half-hour", "one-day", "largest"],
)
def test_staging_expiry_outlives_both_result_waits(wait: float, lifetime: timedelta) -> None:
    client = _FakeClient()
    sink_module.BigQuerySink(TABLE_ID, client=client, job_wait_timeout=wait).write([_row()])
    staging, _ = client.creates[0]
    assert staging.expires == NOW + lifetime
    assert client.waits == [("load", float(wait)), ("query", float(wait))]


@pytest.mark.parametrize("phase", ["load.result", "query.result"])
def test_interruptions_still_cleanup_owned_staging(phase: str) -> None:
    client = _FakeClient()
    interrupt = KeyboardInterrupt("Synthetic interruption")
    client.failures[phase] = interrupt
    with pytest.raises(KeyboardInterrupt) as raised:
        sink_module.BigQuerySink(TABLE_ID, client=client).write([_row()])
    assert raised.value is interrupt
    assert client.deletes == [(STAGING_ID, True)]
    assert client.live_tables == {TABLE_ID}


def test_serial_replays_have_separate_staging_and_revalidate_destination() -> None:
    client = _FakeClient()
    sink = sink_module.BigQuerySink(TABLE_ID, client=client)
    sink.write([_row()])
    sink.write([_row()])
    staging_ids = [str(table.reference) for table, _ in client.creates]
    assert len(set(staging_ids)) == 2
    assert all(table_id != TABLE_ID for table_id in staging_ids)
    assert client.lookups == [TABLE_ID, TABLE_ID]
    assert client.deletes == [(table_id, True) for table_id in staging_ids]
    assert client.live_tables == {TABLE_ID}


def test_default_client_is_lazy_configured_and_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    factory_calls: list[tuple[str, str]] = []

    def factory(*, project: str, location: str) -> _FakeClient:
        factory_calls.append((project, location))
        return client

    monkeypatch.setattr(bigquery, "Client", factory)
    sink = sink_module.BigQuerySink(TABLE_ID)
    sink.write([])
    assert factory_calls == []
    sink.write([_row()])
    sink.write([_row()])
    assert factory_calls == [("example-project", "EU")]


def test_client_initialization_failure_is_a_storage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    error = RuntimeError("Synthetic configuration failure")

    def factory(*, project: str, location: str) -> NoReturn:
        raise error

    monkeypatch.setattr(bigquery, "Client", factory)
    with pytest.raises(StorageError, match="initialize the BigQuery client") as raised:
        sink_module.BigQuerySink(TABLE_ID).write([_row()])
    assert raised.value.__cause__ is error


class _OneDaySource:
    def fetch_availability(
        self, hotel_id: str, start_date: date, end_date: date
    ) -> Iterator[dict[str, object]]:
        yield {"room_type_id": "double", "date": start_date.isoformat(), "available": 4}


def test_cli_shows_cleanup_notes_but_never_exception_causes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = _FakeClient()
    client.failures["load"] = BadRequest("load-cause-canary")  # type: ignore[no-untyped-call]
    client.failures["delete_table"] = RuntimeError("cleanup-cause-canary")
    monkeypatch.setattr(bigquery, "Client", lambda **_settings: client)
    monkeypatch.setattr(cli, "HttpAvailabilityClient", lambda _url, _token: _OneDaySource())
    monkeypatch.setenv("HOTEL_API_BASE_URL", "https://api.example.invalid/v1")
    monkeypatch.setenv("HOTEL_API_TOKEN", "synthetic-token")
    config = tmp_path / "hotels.json"
    hotels = [{"hotel_id": "synthetic_hotel", "room_type_ids": ["double"]}]
    config.write_text(json.dumps({"hotels": hotels}), encoding="utf-8")
    arguments = ["sync", "--config", str(config), "--days", "1"]
    arguments += ["--snapshot-at", "2026-09-27T12:00:00Z", "--bigquery", TABLE_ID]
    assert cli.main(arguments) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)["error"]
    assert error.startswith("BigQuery staging load failed before the merge was submitted")
    assert LOAD_JOB_ID in error
    assert error.endswith(
        f"Staging cleanup also failed; staging table {STAGING_ID} expires at {STAGING_EXPIRY_TEXT}."
    )
    assert "canary" not in captured.err


class _HttpResponse:
    """The parts of requests.Response that the SDK reads, including for API errors."""

    def __init__(
        self, status: int, body: object | None = None, request: object | None = None
    ) -> None:
        self.status_code = status
        self.content = b"" if body is None else json.dumps(body).encode()
        self.text = self.content.decode()
        self.headers = {"content-type": "application/json"}
        self.reason = "OK"
        self.request = request

    def json(self) -> Any:
        return json.loads(self.content)


class _FakeBigQueryHttp:
    """Answer a real SDK client's REST calls in process; no socket is opened.

    With ``failure_reason`` set, the first query job ends with that error reason,
    which the SDK's default job retry would treat as retryable. With
    ``lose_first_insert_response`` set, the first query job is created but its
    insert response is lost, as a dropped connection would lose it.
    """

    is_mtls = False

    def __init__(
        self, failure_reason: str | None = None, *, lose_first_insert_response: bool = False
    ) -> None:
        self.failure_reason = failure_reason
        self.lose_first_insert_response = lose_first_insert_response
        self.calls: list[str] = []
        self.timeouts: list[tuple[str, object]] = []
        self.staging_tables: list[dict[str, Any]] = []
        self.load_jobs: list[dict[str, Any]] = []
        self.query_jobs: list[dict[str, Any]] = []

    def _record(self, call: str, options: Mapping[str, object]) -> None:
        self.calls.append(call)
        self.timeouts.append((call, options.get("timeout")))

    def request(self, method: str, url: str, data: Any = None, **options: object) -> _HttpResponse:
        path = urlsplit(url).path
        project = "/bigquery/v2/projects/example-project"
        tables = f"{project}/datasets/analytics/tables"
        if method == "GET" and path == f"{tables}/availability":
            self._record("get destination", options)
            return _HttpResponse(200, _destination().to_api_repr())
        if method == "POST" and path == tables:
            self._record("create staging", options)
            self.staging_tables.append(json.loads(data))
            return _HttpResponse(200, self.staging_tables[-1])
        if method == "POST" and path == "/upload/bigquery/v2/projects/example-project/jobs":
            self._record("upload load job", options)
            # A multipart upload starts with the job resource as a JSON part.
            text = data.decode()
            metadata, _ = json.JSONDecoder().raw_decode(text, text.index("{"))
            self.load_jobs.append(metadata)
            return _HttpResponse(200, {**metadata, "status": {"state": "DONE"}})
        if method == "POST" and path == f"{project}/jobs":
            self._record("insert query job", options)
            job = json.loads(data)
            if any(job["jobReference"] == known["jobReference"] for known in self.query_jobs):
                error = {"reason": "duplicate", "message": "Synthetic job already exists"}
                body = {"error": {"code": 409, "message": error["message"], "errors": [error]}}
                return _HttpResponse(409, body, SimpleNamespace(method=method, url=url))
            self.query_jobs.append(job)
            if self.lose_first_insert_response and len(self.query_jobs) == 1:
                raise requests.exceptions.ConnectionError("Synthetic lost insert response")
            return _HttpResponse(200, {**job, "status": {"state": "RUNNING"}})
        if method == "GET" and path.startswith(f"{project}/jobs/"):
            self._record("get query job", options)
            ids = [job["jobReference"]["jobId"] for job in self.query_jobs]
            position = ids.index(path.rsplit("/", 1)[1])
            status: dict[str, object] = {"state": "DONE"}
            if self.failure_reason is not None and position == 0:
                error = {"reason": self.failure_reason, "message": "Synthetic job failure"}
                status = {"state": "DONE", "errorResult": error, "errors": [error]}
            return _HttpResponse(200, {**self.query_jobs[position], "status": status})
        if method == "GET" and path.startswith(f"{project}/queries/"):
            self._record("get query results", options)
            reference = {"projectId": "example-project", "jobId": path.rsplit("/", 1)[1]}
            return _HttpResponse(200, {"jobReference": reference, "jobComplete": True})
        if method == "DELETE" and path == f"{tables}/{STAGING_ID.rsplit('.', 1)[1]}":
            self._record("delete staging", options)
            return _HttpResponse(204)
        raise AssertionError(f"Unexpected BigQuery request: {method} {path}")


def _sdk_client(http: _FakeBigQueryHttp) -> bigquery.Client:
    credentials = AnonymousCredentials()  # type: ignore[no-untyped-call]
    # The SDK annotates _http as requests.Session; these code paths use only
    # request() and is_mtls from it.
    return _SDK_CLIENT(
        project="example-project", credentials=credentials, _http=cast(Any, http), location="EU"
    )


def test_real_sdk_client_over_fake_http_completes_the_write_sequence() -> None:
    http = _FakeBigQueryHttp()
    sink_module.BigQuerySink(TABLE_ID, client=_sdk_client(http)).write([_row()])
    assert http.calls == [
        "get destination",
        "create staging",
        "upload load job",
        "insert query job",
        "get query job",
        "get query results",
        "delete staging",
    ]
    # Small table and job-insert requests get a transport timeout and the upload
    # keeps the SDK default. The query wait passes job_wait_timeout to each status
    # request; the load returned DONE in its upload response, so it needed no poll.
    assert http.timeouts == [
        ("get destination", REQUEST_TIMEOUT),
        ("create staging", REQUEST_TIMEOUT),
        ("upload load job", None),
        ("insert query job", REQUEST_TIMEOUT),
        ("get query job", 300.0),
        ("get query results", 300.0),
        ("delete staging", REQUEST_TIMEOUT),
    ]
    staging = http.staging_tables[0]
    assert staging["tableReference"]["tableId"] == STAGING_ID.rsplit(".", 1)[1]
    assert staging["expirationTime"] == str(int((NOW + timedelta(hours=1)).timestamp() * 1000))
    load = http.load_jobs[0]["jobReference"]
    assert load == {"projectId": "example-project", "jobId": LOAD_JOB_ID, "location": "EU"}
    job = http.query_jobs[0]
    assert job["jobReference"]["jobId"].startswith(MERGE_JOB_PREFIX)
    assert job["jobReference"]["location"] == "EU"
    settings = job["configuration"]["query"]
    assert settings["useLegacySql"] is False
    assert settings["maximumBytesBilled"] == "1073741824"
    assert settings["queryParameters"][0]["name"] == "snapshot_dates"
    assert settings["query"].lstrip().startswith("BEGIN TRANSACTION;")


@pytest.mark.parametrize("reason", ["rateLimitExceeded", "jobBackendError"])
def test_real_sdk_client_does_not_resubmit_a_failed_merge_job(reason: str) -> None:
    http = _FakeBigQueryHttp(failure_reason=reason)
    with pytest.raises(StorageError, match="transactional merge failed") as raised:
        sink_module.BigQuerySink(TABLE_ID, client=_sdk_client(http)).write([_row()])
    assert isinstance(raised.value.__cause__, GoogleAPICallError)
    assert http.calls.count("insert query job") == 1
    assert http.calls[-1] == "delete staging"
    failed_job = http.query_jobs[0]["jobReference"]["jobId"]
    assert f"Check query job {failed_job} in location EU" in str(raised.value)


def test_real_sdk_client_recovers_its_merge_job_after_a_lost_insert_response() -> None:
    http = _FakeBigQueryHttp(lose_first_insert_response=True)
    sink_module.BigQuerySink(TABLE_ID, client=_sdk_client(http)).write([_row()])
    # The SDK retried the insert with the same job ID, received 409, and fetched the
    # job that already existed. A fixed job_id would have surfaced that 409 instead.
    assert http.calls == [
        "get destination",
        "create staging",
        "upload load job",
        "insert query job",
        "insert query job",
        "get query job",
        "get query results",
        "delete staging",
    ]
    assert len(http.query_jobs) == 1
    assert http.query_jobs[0]["jobReference"]["jobId"].startswith(MERGE_JOB_PREFIX)
