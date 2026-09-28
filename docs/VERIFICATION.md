# Verification

## Run the checks

Use the locked development environment:

```sh
uv sync --locked --all-extras --python 3.12
uv run --locked --all-extras ruff check .
uv run --locked --all-extras ruff format --check .
uv run --locked --all-extras mypy
uv run --locked --all-extras pytest --cov=hotel_etl --cov-branch --cov-report=term-missing
```

The [CI workflow](../.github/workflows/verify.yml) runs those checks and the local demo on Windows and Ubuntu with Python 3.12 and 3.13. [Published run history](https://github.com/PellaML/hotel-etl-reference/actions/workflows/verify.yml) identifies which revisions passed. Test counts and coverage belong to a specific revision; use the command output rather than treating a number in prose as a guarantee.

## Test scope

- Record and configuration validation, including dates, integer bounds and identifiers.
- Complete hotel/room/date coverage and duplicate/conflicting source records.
- Real loopback HTTP responses, pagination, response framing, bounded retries and public-error redaction.
- Loopback proxy bypass and fresh request objects for HTTPS retries.
- SQLite schema/key-collation checks, transactional rollback, replay and observation ordering.
- BigQuery orchestration with a method-level fake client and the real SDK over fake HTTP, including recovery after a lost job-insert response. This checks request/configuration behavior, not execution by the GoogleSQL engine.
- Full OpenAPI 3.1 validation, captured responses and fixture behavior. The [Swagger guide](api/README.md) explains the documentation checks and local viewer.
- CLI error output and a demo subprocess with site-packages disabled.

Runtime tests deny non-loopback socket connections. Dependency installation is a separate network operation; the test suite itself does not use production credentials or call a live cloud project.

## Installed-package smoke check

Build and test from a clean source tree. Use a new output directory because the demo does not overwrite existing files.

```sh
uv build --wheel --out-dir output/dist
python -m venv output/smoke-venv
```

Use the smoke environment's Python for these commands:

```sh
python -m pip install --no-index --no-deps output/dist/hotel_etl_reference-0.1.0-py3-none-any.whl
python -m hotel_etl demo --output-dir output/wheel-demo
```

Check the reported counts: 2,190 after the first write, 2,190 after identical replay, and 4,380 after the next snapshot day. This proves the installed local example works without the optional Google SDK; it does not validate a vendor integration.

## Performance measurements

The checked-in [benchmark sample](benchmark-current.json) records a synthetic in-process source, validation and two SQLite writes with memory tracing enabled. `scripts/benchmark.py` reproduces that workload. Tracemalloc affects runtime and measures Python allocations, not total process RSS. Desktop timings are illustrative and are not API throughput, cloud cost or a service-level guarantee.

## Remaining validation

Before a real deployment, validate the vendor contract, token flow, pagination consistency, timezone and completeness rules. Separately execute the BigQuery statements in the intended test dataset and check permissions, query cost, cleanup, timeout/unknown completion and single-writer operation.

Live vendor behavior, GoogleSQL, production IAM/billing, Cloud Run and Scheduler are not covered by the local tests. The Docker image and macOS execution have not been verified. Tests do not establish bug-free operation or distributed exactly-once delivery.
