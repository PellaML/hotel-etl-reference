# Deployment and cloud acceptance boundary

## Status

This repository includes an executable adapter and an image recipe, not a deployed or production-certified cloud service. No live hotel API, BigQuery dataset, Cloud Run execution, Scheduler job, identity, billing account or cloud IAM policy was created or accessed while producing the reference. The commands on this page are templates that have not been run.

The `Dockerfile` defaults to `sync --help`, not a write. Its cloud dependencies are version- and hash-locked in `requirements-cloud.txt`; `uv.lock` also locks the development environment. The base Python image tag can move: pin a verified image digest during the actual deployment and refresh it through the owner's vulnerability-management process.

## Required information before a paid live pilot

1. Vendor API documentation (for example a Swagger or OpenAPI description), demo access, canonical URL, token issuance and expiry, pagination and rate limits.
2. Authoritative hotel and room-type IDs, and whether zero inventory is explicit or represented by missing records.
3. Meaning of availability (physical rooms, sellable rooms, allotments, overbooking), business timezone, stay-horizon boundaries and source consistency across pages.
4. A client-owned GCP project and an explicitly approved cost ceiling, dataset region and test table. Credentials should be granted through IAM or Secret Manager, never sent in a chat or committed.
5. Whether daily history should keep the latest observation per day (this sample's behavior), immutable first observations, or every intraday observation.

Reservation updates and cancellations, and small dictionary tables, belong to a later, separately scoped integration; they are not demonstrated here.

## Destination and minimal access

Have the owner create the dataset and test table from `examples/bigquery-schema.sql` after substituting identifiers. The dataset location must equal the `--location` value, which defaults to `EU`. The adapter checks that all six fields are REQUIRED with the specified types, that the destination is an ordinary table in the matching location, and that it is DAY-partitioned on `snapshot_date`. It refuses incompatible tables instead of migrating or replacing them. The sample DDL also enables a partition-filter requirement and clustering.

Each write also creates a table named `_hotel_etl_stage_<run>` in the destination dataset, loads the batch into it, merges it into the target and then deletes it.

An implementation commonly needs a dedicated runtime service account with:

- BigQuery Job User on the project named in the table ID. The CLI creates its client for that project, so the load and query jobs run and are billed there.
- BigQuery Data Editor on the destination dataset, not the project or organization. This covers writes to the existing target and the creation and deletion of the staging tables. Granted on a dataset, the role can also change or delete every other table in it, so keep unrelated tables out of that dataset.
- Secret Manager Secret Accessor on the one API-token secret, if Secret Manager is used.
- No service-account key in the image, repository or job configuration. The attached identity supplies Application Default Credentials. Do not set `GOOGLE_APPLICATION_CREDENTIALS` in the job, because Application Default Credentials check it before the attached identity.

An operator who investigates a failed run needs to see jobs created by that identity, for example through BigQuery Resource Viewer on the same project.

The owner must verify these permissions against organizational policy. These notes are not evidence that a given IAM setup has been exercised.

## Cloud Run job recipe

The container runs a batch job, not an HTTP web server. Build the image in an owner-approved build environment; building and pushing images can incur charges. Replace every placeholder below and have the owner approve the resources and costs before running anything.

```bash
gcloud run jobs create hotel-availability-sync \
  --region=REGION \
  --image=IMAGE@sha256:DIGEST \
  --service-account=RUNTIME_SA_EMAIL \
  --tasks=1 \
  --parallelism=1 \
  --max-retries=0 \
  --task-timeout=TASK_TIMEOUT \
  --set-env-vars=HOTEL_API_BASE_URL=https://API_HOST/v1 \
  --set-secrets=HOTEL_API_TOKEN=SECRET_NAME:SECRET_VERSION \
  --args=--config,/app/config/hotels.json,--bigquery,PROJECT_ID.DATASET.availability_snapshot,--location,EU,--days,365
```

- The image's entry point already supplies `sync`; `--args` replaces the default `--help`.
- Supply the hotel configuration as a read-only file at the path given to `--config`, for example in a derived image or a mounted volume.
- The CLI reads the token only from the `HOTEL_API_TOKEN` environment variable, not from a mounted secret file. Cloud Run resolves secrets in environment variables when an instance starts, and Google recommends pinning such a secret to a specific version rather than `latest`.
- Cloud Run retries a failed task three times by default and stops a task after 10 minutes. Keep `--max-retries=0`: a retried task starts a new write while an earlier MERGE may still be running. Set `--task-timeout` above the expected source fetch time plus both BigQuery waits (300 seconds each by default), with a margin for request retries.
- Attaching `--service-account` requires the deploying user to have Service Account User on that account.

Start executions through the Cloud Run Admin API with a separate Scheduler identity. Requests to `*.googleapis.com` need an OAuth token, not an OIDC token. Grant that identity Cloud Run Invoker on this job only:

```bash
gcloud run jobs add-iam-policy-binding hotel-availability-sync \
  --region=REGION \
  --member=serviceAccount:SCHEDULER_SA_EMAIL \
  --role=roles/run.invoker

gcloud scheduler jobs create http hotel-availability-daily \
  --location=SCHEDULER_REGION \
  --schedule="SCHEDULE" \
  --time-zone=TIME_ZONE \
  --uri=https://run.googleapis.com/v2/projects/PROJECT_ID/locations/REGION/jobs/hotel-availability-sync:run \
  --http-method=POST \
  --oauth-service-account-email=SCHEDULER_SA_EMAIL \
  --max-retry-attempts=0
```

- Creating the Scheduler job requires Service Account User on the Scheduler identity.
- Keep Scheduler retries at 0, the default. A retried request can start a second execution.
- Select an explicit timezone for the schedule. Changing Scheduler's timezone does not change this sample's UTC snapshot-date semantics.

### Single writer is a requirement, not a distributed lock

`parallelism=1` only constrains tasks within an execution. It does not prevent two Scheduler or manual executions, or a retry, from overlapping. Cloud Scheduler is designed for at-least-once delivery, so in rare cases one scheduled time can start two executions even with retries disabled. Permit one active execution per destination, verify that earlier executions have finished before a manual replay, and add an owner-approved distributed lock if overlapping triggers must be supported. Without that, do not deploy this sample into a concurrent-writer workflow.

BigQuery does not enforce primary-key uniqueness, and a MERGE that only inserts rows does not conflict with concurrent DML. Two overlapping first writes for the same snapshot day can therefore both commit and leave duplicate keys. The script checks the affected target partitions for duplicate keys before merging, so later writes for that day stop until an operator repairs the partition. It also refuses conflicting values for the same observation timestamp and merges within a transaction. These checks are not a promise of distributed exactly-once behavior.

## Failure and cost controls

- All configured hotels must pass completeness validation before the destination sees a batch. Source failures do not produce partial snapshots.
- HTTP body, page, record and retry limits are bounded. The HTTP timeout applies to each I/O operation, not to a whole streaming response, so the job needs its own execution deadline.
- BigQuery table requests and the query-job insert have a 60-second timeout per attempt, and the SDK's default retry policy can repeat them for up to 10 minutes. The staging upload keeps the SDK default of no per-request timeout, so only the task timeout bounds a stalled upload.
- Each load or query result wait is limited by `job_wait_timeout` (300 seconds by default). The SDK checks this limit between status requests, and each status request has its own transport timeout, so a slow or retried request can extend a wait. A wait timeout does not cancel the BigQuery job.
- The query sets a maximum bytes billed limit (1 GiB by default). Under on-demand pricing, BigQuery refuses a query that it estimates would exceed the limit and does not charge for it, but statements that already ran in this multi-statement script are still billed. The limit does not cap reservation or slot costs. For clustered tables the estimate is an upper bound, so a query can be refused even when its actual bytes would fit. On-demand billing also has a 10 MB minimum per referenced table.
- Both limits are constructor arguments. The CLI always uses the defaults, so other owner-approved values need a small code change or wrapper.
- Load jobs are named `hotel_etl_load_<run>` and MERGE query jobs start with `hotel_etl_merge_<run>_`, where `<run>` matches the staging table name.
- A failure before the MERGE job is submitted (staging creation, upload or load) leaves the destination unchanged, and the error says so. After submission, a timeout or lost response can mean the job is still running or already committed. The error then names the query job, or the job ID prefix if the insert itself failed. Check it before any replay:

  ```bash
  bq show --format=prettyjson --job=true PROJECT_ID:LOCATION.JOB_ID
  bq ls --jobs=true --all=true --max_results=50 PROJECT_ID
  ```

  `DONE` with an `errorResult` means the script failed and BigQuery rolled back its open transaction. `DONE` without an `errorResult` means the script finished, including `COMMIT TRANSACTION`. Any other state means the job is still pending or running.
- Each staging table expires one hour after it is created, or later when `job_wait_timeout` is raised, so it outlives both waits. Only an acknowledged, run-owned staging table is deleted. If creation is unacknowledged, expiration is the cleanup fallback, which avoids deleting an unrelated table with the same name.
- A cleanup failure is reported with the staging table name and expiry time. A cleanup failure after a committed merge is reported distinctly; do not interpret it as proof that no data was written.
- Table partitioning checks and query parameters are tested structurally with a fake client. Actual query cost and partition pruning have not been measured. Billing alerts are useful but are not a hard spending limit.
- Log run status and counts, not API tokens, raw payloads or guest data. Review any SDK debug logging before enabling it.

## Live pilot acceptance checklist

A buyer should not accept this as a live integration solely because local tests pass. In a buyer-owned test environment, record:

- [ ] Actual API authentication, pagination and field mapping confirmed against the API documentation.
- [ ] Full configured room and date coverage, including zero inventory; sample counts reconciled with the source.
- [ ] Correct business-time and date behavior agreed and tested at day and year boundaries.
- [ ] Actual load and GoogleSQL script executed against the approved region and schema within the approved cost cap.
- [ ] Both `ASSERT` guards run inside the transaction, and a failing guard rolls it back. BigQuery's transaction documentation lists SELECT, DML and temporary-table statements as supported and does not mention `ASSERT`.
- [ ] The `IN UNNEST(@snapshot_dates)` filter satisfies the partition-filter requirement, and the job statistics show that only the affected partitions were scanned. Confirm from the same statistics how the maximum bytes billed limit applies across the script's statements.
- [ ] Replay of the same observation adds no duplicate keys; a later same-day observation updates as agreed; an earlier one cannot overwrite it.
- [ ] Two observation days remain queryable for the same future stay dates.
- [ ] Authentication, rate-limit and partial-page failures leave the target unchanged and produce a redacted, actionable error.
- [ ] Staging cleanup and expiry, IAM denial, job timeout and ambiguous completion exercised and documented, including a lookup of the job named in the error.
- [ ] Cloud Run execution, scheduling, single-writer operation and alerting verified, with owner handover.

Until those checks are completed, the honest deliverable is a tested local reference plus a cloud adapter whose service boundary remains unverified.

The adapter explicitly disables automatic resubmission of failed MERGE query jobs. The SDK may still retry individual API calls, and a retried job insert reuses its job ID. Because the MERGE uses a job ID prefix rather than a fixed ID, the SDK then fetches the job it already created instead of failing on the duplicate. This is tested with a real SDK client over a fake transport; no query is executed against BigQuery during those tests.