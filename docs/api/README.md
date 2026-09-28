# Synthetic fixture API reference

[openapi.json](openapi.json) documents the synthetic input fixture, not a vendor API, hosted demo or ETL web-service rewrite. [Swagger](index.html) is read-only. Client/pipeline guarantees are in [API_CONTRACT.md](../API_CONTRACT.md).

## View locally

From the project root:

~~~sh
python -m http.server 8765 --bind 127.0.0.1 --directory docs/api
~~~

Open [http://127.0.0.1:8765/](http://127.0.0.1:8765/); stop with Ctrl+C. Serve only this directory. This stdlib server is not for deployment. Use HTTP: file:// can block adjacent JSON fetching.

The fixture starts separately on an ephemeral loopback port and stops with its Python context. Port 0 means unconfigured, not the documentation server.

## Read-only, privacy and offline choices

The default view is local-only. The Swagger button downloads two pinned 5.33.0 assets from jsDelivr, revealing connection metadata such as IP address and browser headers. Referrers are suppressed; no spec, hotel data or credentials are uploaded. Never enter real credentials.

Submission, authorization controls/persistence, remote validation and URL-based configuration are disabled. The adjacent spec loads without credentials or redirects. External references/examples are refused, and an interceptor rejects Swagger requests.

CSP allows local resources and the exact CDN paths, blocking nonlocal fetch/XHR, forms, child frames and workers. Neither unsafe-inline nor unsafe-eval is allowed. The early meta-CSP governs only following content, not separately opened JSON. It cannot enforce frame-ancestors, sandbox, report-uri or report-only mode, or create HTTP security headers. It is not clickjacking protection.

[Hashes](swagger-ui-assets.json) and [license notes](THIRD_PARTY.md) record source verification and SHA-384 integrity. CDN loads use anonymous CORS. SRI pins bytes, not script trust. Offline Swagger needs about 1.77 MB of local assets, retained license/notices and allowlist/CSP updates. No assets are vendored; browser cache is not an offline guarantee. The local JSON view needs no CDN.

## Exact fixture behavior

### Request and authentication

GET /availability requires hotel_id, start_date and end_date; cursor is optional. There is no request body. Accept is not negotiated: handled GET responses are application/json with byte-accurate Content-Length.

~~~text
Authorization: Bearer synthetic-demo-token-not-a-secret
~~~

The first parsed Authorization value must match exactly; header names are case-insensitive. Duplicate Authorization fields are not independently validated. The token is public. HOTEL_API_TOKEN configures the client, not the fixture. GET authentication precedes path/query checks; failures return 401 without WWW-Authenticate.

### Parameters and parsing

- IDs are registered Hotel IDs: 1-128 ASCII characters, starting with a letter/digit, then letters, digits, underscore, hyphen or dot. Built-ins DEMO_NORTH and DEMO_SOUTH each have single, double and suite; custom validated configurations are allowed.
- Dates are inclusive, in years 0001-9999. The end can be at most 365 days after the start: 1-366 dates. Prefer YYYY-MM-DD. Python date.fromisoformat also accepts compact dates (20260927) and ISO week dates (2026-W39-7, 2026W397); omitted weekdays mean Monday. Calendar and cross-field validity require semantic checks beyond spelling patterns.
- Cursor is an integer offset from 0 through room count times included dates, inclusive. The 1098 maximum applies only to built-in three-room hotels. Python int() also accepts zero padding, an encoded plus sign/whitespace and digit separators. Prefer plain digits. There is no client-style 4096-character cap; Python integer-conversion and HTTP parsing limits still apply.
- Blank query values are discarded: required blanks act as missing; a blank cursor means 0. Multiple surviving values fail for any key, even unknown keys. One blank plus one nonblank occurrence is accepted. Unknown single values are ignored, including page_size. Bare fields without = and empty & segments are malformed.

### Pages, data and errors

The helper's Python page_size argument defaults to 128. A positive integer is a caller precondition, not validated. Zero can repeat an empty page's cursor, which the client refuses. Records regenerate in configured room order, then date order, and are sliced at cursor. Cursors are not signed/query-bound; keep hotel and dates unchanged.

A 200 body has exactly items and next_cursor. Items have exactly room_type_id, date and available, not hotel_id or snapshot/observation fields. Dates render as YYYY-MM-DD. Counts are integers 0-11, including explicit zeroes:

~~~text
(sum(ord(c) for c in hotel_id + room_type_id)
 + observation_date.toordinal() + stay_date.toordinal()) % 12
~~~

Observation date is fixed at startup, not the clock/query. Examples use DEMO_HOTELS and observation_date=2026-09-27; x-fixture-request records page size and query.

next_cursor is a decimal string for the next offset, or null. Send it as a URL-encoded value, never a URL. An offset equal to the total returns empty items and null. Stop at null.

| Condition | Response |
| --- | --- |
| Invalid GET path, unknown hotel, missing/invalid date, reversed/overlong range, malformed/duplicate query, unparseable/negative cursor | 400, error: Invalid fixture request |
| Parsed nonnegative offset above the record count | 400, error: Invalid cursor |
| Missing/wrong bearer on any GET | 401, error: Unauthorized |

Errors are JSON objects with one error string. Authenticated GETs to /availability/, /v1/availability or unknown paths return 400, not 404. Unsupported methods, including POST/OPTIONS, receive the standard-library 501 HTML response without GET authentication; HEAD gets 501 without a body. There is no CORS/preflight support. Other standard-library HTTP errors are outside this contract.

The adapter accepts opaque cursors up to 4096 characters and nonnegative int64 counts. Retries, HTTPS, redirect and resource limits are client safeguards, not server enforcement. Extra fields are not globally rejected. The pipeline checks completeness, optional hotel_id and duplicates before writing.

## Required validation

Install the locked development and cloud extras, then run from the project root. Keeping all extras selected avoids removing the BigQuery SDK from an existing environment:

~~~sh
uv sync --locked --all-extras
uv run --offline --locked --all-extras python -m pytest -q -p no:cacheprovider tests/test_openapi.py
uv run --offline --locked --all-extras python -m ruff check --no-cache tests/test_openapi.py
uv lock --check --offline
~~~

A required openapi-spec-validator test checks the complete 3.1 document. OAS31Validator checks schema definitions, defaults, examples and responses with explicit format checking. Missing dependencies fail, not skip. Nonlocal references and HTTP/file retrieval are blocked; pure validation also blocks DNS/connections. Fixture tests use loopback, never cloud services. JSON Schema accepts 1.0 as an integer; a separate assertion checks integer serialization.
