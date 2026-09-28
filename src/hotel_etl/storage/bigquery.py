"""Optional BigQuery sink; importing this module needs only the standard library.

The destination must already exist with exactly the six REQUIRED columns below,
partitioned daily on ``snapshot_date``. Field order and descriptive metadata do
not matter. Only ordinary ``project.dataset.table`` identifiers are supported;
decorators, wildcards, quoted names, and domain-scoped projects are rejected.

Run one writer at a time for a destination, including retries. The transaction
and monotonic MERGE do not provide a distributed lock or an exactly-once claim.
A failure before the MERGE job is submitted leaves the destination unchanged.
After submission, a timeout or lost job response can leave the commit outcome
unknown: a wait timeout does not cancel the job. The SDK's job retry is disabled,
so a failed MERGE job is not resubmitted; the SDK may still retry individual API
requests, and a retried job insert reuses its job ID. Error messages name the job
to verify before any single-writer replay. Cloud deployment and SQL execution
still require verification in the owner's BigQuery project.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from sys import float_info
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from hotel_etl.errors import StorageError, ValidationError
from hotel_etl.models import AvailabilityRow, validate_batch

if TYPE_CHECKING:
    from google.cloud import bigquery

_TABLE_ID = re.compile(
    r"(?P<project>[a-z][a-z0-9-]{4,28}[a-z0-9])\."
    r"(?P<dataset>[A-Za-z_][A-Za-z0-9_]{0,1023})\."
    r"(?P<table>[A-Za-z_][A-Za-z0-9_]{0,1023})"
)
_LOCATION = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,62}")
_SCHEMA = (
    ("hotel_id", "STRING"),
    ("room_type_id", "STRING"),
    ("stay_date", "DATE"),
    ("available_rooms", "INTEGER"),
    ("snapshot_date", "DATE"),
    ("observed_at", "TIMESTAMP"),
)
# The SDK's default per-request timeout is None, so a stalled connection could block
# until an outer deadline kills the process. Table and job-insert requests are small,
# and the SDK's retry policy still decides whether to retry after a timeout. The
# staging upload keeps the default because its body can be large enough for a fixed
# limit to fail on a slow link.
_REQUEST_TIMEOUT = 60.0
# A staging table must outlive both result waits and the requests around them. An
# hour covers the defaults and longer waits extend it. BigQuery documents limits of
# 6 hours for a load job and 24 hours for a multi-statement query, so longer waits
# need no extra lifetime.
_STAGING_MIN_LIFETIME = timedelta(hours=1)
_STAGING_REQUEST_MARGIN = timedelta(minutes=30)
_LONGEST_USEFUL_WAIT = timedelta(hours=24)


class BigQuerySink:
    """Write validated availability batches, under a single-writer restriction.

    A supplied client must implement the BigQuery client methods used here.
    Supplying one avoids credential discovery, which the tests rely on, but the
    optional SDK is still needed for real schema and job-configuration objects on
    nonempty writes.

    ``job_wait_timeout`` defaults to 300 seconds for each load/query result wait,
    not an end-to-end write deadline. The SDK checks it between status requests,
    so one slow or retried request can extend a wait. ``maximum_bytes_billed``
    defaults to 1 GiB on the query job; it is a positive int64 byte count for
    on-demand billing, not a total cloud-spend cap. Both limits are validated
    locally without importing the SDK or doing I/O.
    """

    def __init__(
        self,
        table_id: str,
        *,
        location: str = "EU",
        client: Any | None = None,
        job_wait_timeout: float = 300.0,
        maximum_bytes_billed: int = 1024**3,
    ) -> None:
        match = _TABLE_ID.fullmatch(table_id) if isinstance(table_id, str) else None
        if match is None:
            raise ValidationError(
                "BigQuery table_id must be a bare project.dataset.table identifier "
                "with a valid project ID and simple letter/underscore-led names."
            )
        if not isinstance(location, str) or _LOCATION.fullmatch(location) is None:
            raise ValidationError("BigQuery location must be a nonempty location identifier.")
        if (
            isinstance(job_wait_timeout, bool)
            or not isinstance(job_wait_timeout, (int, float))
            or not 0 < job_wait_timeout <= float_info.max
        ):
            raise ValidationError("job_wait_timeout must be a positive finite number of seconds.")
        if type(maximum_bytes_billed) is not int or not 0 < maximum_bytes_billed <= 2**63 - 1:
            raise ValidationError("maximum_bytes_billed must be a positive int64 byte count.")
        self._job_wait_timeout = float(job_wait_timeout)
        self._staging_lifetime = _staging_lifetime(self._job_wait_timeout)
        self._maximum_bytes_billed = maximum_bytes_billed
        self._table_id = table_id
        self._project = match["project"]
        self._dataset = match["dataset"]
        self._location = location
        self._client = client

    def write(self, rows: Sequence[AvailabilityRow]) -> None:
        """Reject bad batches before I/O; never create or replace the destination."""
        batch = tuple(rows)
        if not batch:
            return
        validate_batch(batch)

        try:
            from google.cloud import bigquery
        except ImportError as exc:
            raise StorageError(
                "BigQuery writes require the optional google-cloud-bigquery>=3.25,<4 dependency."
            ) from exc

        client = self._client
        if client is None:
            try:
                client = bigquery.Client(project=self._project, location=self._location)
            except Exception as exc:
                raise StorageError("Could not initialize the BigQuery client.") from exc
            self._client = client

        try:
            destination = client.get_table(self._table_id, timeout=_REQUEST_TIMEOUT)
        except Exception as exc:
            raise StorageError("Could not read the existing BigQuery destination.") from exc
        self._validate_destination(destination)

        # One run ID names the staging table and both jobs, so an operator can find
        # everything that a failed run created.
        run_id = uuid4().hex
        staging_id = f"{self._project}.{self._dataset}._hotel_etl_stage_{run_id}"
        # The load job ID is fixed in advance so it can be looked up even when the
        # upload response is lost.
        load_job_id = f"hotel_etl_load_{run_id}"
        # The MERGE gets only a prefix. With a fixed job_id the SDK re-raises the 409
        # from a retried insert instead of fetching the job that a lost response had
        # already created.
        merge_job_prefix = f"hotel_etl_merge_{run_id}_"
        schema = [
            bigquery.SchemaField(name, field_type, mode="REQUIRED") for name, field_type in _SCHEMA
        ]
        staging = bigquery.Table(staging_id, schema=schema)
        expires = datetime.now(UTC) + self._staging_lifetime
        staging.expires = expires
        load_config = bigquery.LoadJobConfig(
            schema=schema,
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            create_disposition=bigquery.CreateDisposition.CREATE_NEVER,
            write_disposition=bigquery.WriteDisposition.WRITE_EMPTY,
            autodetect=False,
            ignore_unknown_values=False,
            max_bad_records=0,
        )
        query_config = bigquery.QueryJobConfig(
            use_legacy_sql=False,
            dry_run=False,
            maximum_bytes_billed=self._maximum_bytes_billed,
            query_parameters=[
                bigquery.ArrayQueryParameter(
                    "snapshot_dates", "DATE", sorted({row.snapshot_date for row in batch})
                )
            ],
        )
        created = False
        failure: BaseException | None = None
        operation = "staging creation"
        merge_job_ref = f"query jobs whose IDs start with {merge_job_prefix}"
        try:
            # Never reuse an existing table. If creation is unacknowledged, the staging
            # table's expiry is the fallback: deleting after a failed create could remove
            # a same-named table that this run did not create.
            client.create_table(staging, exists_ok=False, timeout=_REQUEST_TIMEOUT)
            created = True
            operation = "staging load"
            client.load_table_from_json(
                [row.to_dict() for row in batch],
                staging_id,
                job_config=load_config,
                job_id=load_job_id,
                location=self._location,
            ).result(timeout=self._job_wait_timeout)
            operation = "transactional merge"
            # job_retry=None stops the SDK from resubmitting a failed MERGE job on its
            # own; a replay waits until an operator has checked the outcome.
            merge_job = client.query(
                _merge_sql(self._table_id, staging_id),
                job_config=query_config,
                job_id_prefix=merge_job_prefix,
                location=self._location,
                timeout=_REQUEST_TIMEOUT,
                job_retry=None,
            )
            merge_job_ref = f"query job {merge_job.job_id}"
            merge_job.result(timeout=self._job_wait_timeout, job_retry=None)
        except Exception as exc:
            failure = StorageError(self._failure_message(operation, load_job_id, merge_job_ref))
            raise failure from exc
        except BaseException as exc:
            # Preserve interruptions while still attempting staging cleanup.
            failure = exc
            raise
        finally:
            if created:
                try:
                    client.delete_table(staging_id, not_found_ok=True, timeout=_REQUEST_TIMEOUT)
                except Exception as exc:
                    leftover = f"staging table {staging_id} expires at {expires:%Y-%m-%dT%H:%M:%SZ}"
                    if failure is not None:
                        failure.add_note(f"Staging cleanup also failed; {leftover}.")
                    else:
                        raise StorageError(
                            f"BigQuery merge completed, but staging cleanup failed; {leftover}."
                        ) from exc

    def _failure_message(self, operation: str, load_job_id: str, merge_job_ref: str) -> str:
        """Say whether the destination could have changed, and which job to check."""
        if operation == "transactional merge":
            return (
                "BigQuery transactional merge failed; job outcome may be unknown. "
                f"Check {merge_job_ref} in location {self._location} before any replay "
                "under the single-writer rule; this sink does not automatically retry."
            )
        # Nothing reaches the destination until the MERGE job is submitted, so a failed
        # creation or load cannot have changed it, whatever state the load job is in.
        load_detail = f" Load job: {load_job_id}." if operation == "staging load" else ""
        return (
            f"BigQuery {operation} failed before the merge was submitted, so this run "
            f"did not change the destination.{load_detail} This sink does not "
            "automatically retry."
        )

    def _validate_destination(self, table: bigquery.Table) -> None:
        if (table.project, table.dataset_id, table.table_id) != tuple(self._table_id.split(".")):
            raise StorageError("BigQuery destination metadata does not match the requested table.")
        if table.table_type != "TABLE":
            raise StorageError("BigQuery destination must be an existing ordinary table.")
        if table.location is None or table.location.casefold() != self._location.casefold():
            raise StorageError(
                "BigQuery destination location does not match the configured location."
            )
        actual_schema = {
            field.name: (
                "INTEGER" if field.field_type == "INT64" else field.field_type,
                field.mode,
            )
            for field in table.schema
        }
        expected_schema = {name: (field_type, "REQUIRED") for name, field_type in _SCHEMA}
        if (
            len(table.schema) != len(_SCHEMA)
            or actual_schema != expected_schema
            or table.to_api_repr().get("defaultCollation")
            or any(
                field.fields
                or field.max_length is not None
                or field.default_value_expression is not None
                or field.to_api_repr().get("collation")
                for field in table.schema
            )
        ):
            raise StorageError(
                "BigQuery destination must have exactly the six REQUIRED availability columns "
                "with the expected types, without nested fields, length limits, "
                "defaults, or collation."
            )
        partitioning = table.time_partitioning
        if (
            table.range_partitioning is not None
            or partitioning is None
            or partitioning.field != "snapshot_date"
            or partitioning.type_ != "DAY"
        ):
            raise StorageError("BigQuery destination must be DAY-partitioned on snapshot_date.")


def _staging_lifetime(wait_seconds: float) -> timedelta:
    """Cover the load wait, the MERGE wait and the requests around them."""
    wait = timedelta(seconds=min(wait_seconds, _LONGEST_USEFUL_WAIT.total_seconds()))
    return max(_STAGING_MIN_LIFETIME, 2 * wait + _STAGING_REQUEST_MARGIN)


def _merge_sql(destination: str, staging: str) -> str:
    """Only validated/generated identifiers enter SQL; dates are bound parameters."""
    return f"""
BEGIN TRANSACTION;

-- BigQuery does not enforce primary-key uniqueness. Refuse corrupted partitions.
ASSERT NOT EXISTS (
    SELECT 1
    FROM `{destination}` AS T
    WHERE T.snapshot_date IN UNNEST(@snapshot_dates)
    GROUP BY T.hotel_id, T.room_type_id, T.snapshot_date, T.stay_date
    HAVING COUNT(*) > 1
) AS 'Duplicate availability keys in destination';

ASSERT NOT EXISTS (
    SELECT 1
    FROM `{destination}` AS T
    INNER JOIN `{staging}` AS S
      ON T.hotel_id = S.hotel_id
     AND T.room_type_id = S.room_type_id
     AND T.snapshot_date = S.snapshot_date
     AND T.stay_date = S.stay_date
    WHERE T.snapshot_date IN UNNEST(@snapshot_dates)
      AND S.snapshot_date IN UNNEST(@snapshot_dates)
      AND T.observed_at = S.observed_at
      AND T.available_rooms != S.available_rooms
) AS 'Conflicting availability for the same observation';

MERGE `{destination}` AS T
USING (
    SELECT hotel_id, room_type_id, stay_date, available_rooms, snapshot_date, observed_at
    FROM `{staging}`
    WHERE snapshot_date IN UNNEST(@snapshot_dates)
) AS S
ON T.snapshot_date IN UNNEST(@snapshot_dates)
AND T.hotel_id = S.hotel_id
AND T.room_type_id = S.room_type_id
AND T.snapshot_date = S.snapshot_date
AND T.stay_date = S.stay_date
WHEN MATCHED AND S.observed_at > T.observed_at THEN
    UPDATE SET available_rooms = S.available_rooms, observed_at = S.observed_at
WHEN NOT MATCHED THEN
    INSERT (hotel_id, room_type_id, stay_date, available_rooms, snapshot_date, observed_at)
    VALUES (
        S.hotel_id, S.room_type_id, S.stay_date, S.available_rooms, S.snapshot_date, S.observed_at
    );

COMMIT TRANSACTION;
"""
