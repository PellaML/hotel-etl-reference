# Architecture

The package is a synchronous batch importer, not a web service. It reads a complete snapshot, validates it, and then hands the records to a destination adapter.

## Data flow

1. Load the configured hotel and room-type identifiers.
2. Read each hotel's paginated availability for the requested stay dates.
3. Validate records and complete room/date coverage across the batch.
4. Sort the canonical records and write one snapshot.

The record key is `(hotel_id, room_type_id, snapshot_date, stay_date)`. A newer observation updates the same day's value; earlier snapshot days remain in storage. This is daily history, not a log of every intraday change.

Missing records are not zero availability. Duplicate records with identical values are collapsed during collection; conflicting duplicates abort it. Collection finishes before a sink is called, so an incomplete source response cannot result in a partially accepted batch.

## Modules

| Module | Responsibility |
| --- | --- |
| `cli.py` | Commands, environment configuration and result/error output |
| `config.py` | Hotel configuration files |
| `models.py` | Validated records and canonical date/timestamp serialization |
| `pipeline.py` | Collection, coverage checks and sink orchestration |
| `api.py` | Bearer-authenticated requests, bounded retries and pagination |
| `http_transport.py` | HTTP framing checks and bounded body reads |
| `jsonio.py` | JSON decoding with duplicate-key and non-standard numeric-constant rejection |
| `storage/sqlite.py` | Schema checks, staging and transactional SQLite writes |
| `storage/bigquery.py` | BigQuery preflight, staging, queries and cleanup |
| `fixtures.py`, `demo.py` | Synthetic data, loopback server and repeatable local example |

`AvailabilitySource` and `SnapshotSink` define the integration boundaries. Storage adapters share the record model, but retain separate transaction and cleanup logic because SQLite and BigQuery do not have identical execution semantics.

## Design choices

- The core uses the standard library. BigQuery uses Google's official optional SDK.
- Collection checks expected room/date coverage. Sinks separately check record types and duplicate keys; they cannot infer coverage without the hotel configuration.
- SQL values are bound rather than interpolated. BigQuery resource identifiers are validated before they enter SQL.
- Redirects are refused when sending the source bearer token. Response bodies, pages, records and retry attempts have explicit limits.
- Public errors omit request URLs, response bodies and underlying SDK exception text. Do not enable verbose transport logs with real credentials without checking their redaction behavior.
- The loopback fixture server exists only for the demo and tests. Do not expose it as a production API.

An HTTP administration interface, durable job queue or user-account system would be a separate application around this library, not a replacement for its ingestion rules. See [the API contract](API_CONTRACT.md) and [deployment notes](DEPLOYMENT.md) for the current boundaries.
