-- Example only. Replace the identifiers and run it only with the cloud owner's
-- approval of execution and cost. No cloud dataset or table was created for this
-- reference.

-- The dataset location must equal the adapter's --location value (EU by default).
-- Skip this statement if the owner already has a dataset for the pipeline.
CREATE SCHEMA IF NOT EXISTS `YOUR_PROJECT.YOUR_DATASET`
OPTIONS (location = 'EU');

-- The adapter requires exactly these six REQUIRED columns and daily partitions on
-- snapshot_date. Clustering and the partition filter requirement are recommended
-- but not checked. IF NOT EXISTS leaves an existing table unchanged; the adapter
-- still checks it before each write. Each write also creates and deletes a
-- short-lived table named _hotel_etl_stage_<run> in this dataset.
CREATE TABLE IF NOT EXISTS `YOUR_PROJECT.YOUR_DATASET.availability_snapshot`
(
  hotel_id STRING NOT NULL,
  room_type_id STRING NOT NULL,
  stay_date DATE NOT NULL,
  available_rooms INT64 NOT NULL,
  snapshot_date DATE NOT NULL,
  observed_at TIMESTAMP NOT NULL
)
PARTITION BY snapshot_date
CLUSTER BY hotel_id, room_type_id
OPTIONS (require_partition_filter = TRUE);
