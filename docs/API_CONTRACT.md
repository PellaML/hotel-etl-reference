# Source adapter contract

This is a synthetic input contract, not an integration with a real hotel vendor. It describes what the client accepts and the pipeline requires.

The exact loopback fixture is described in [OpenAPI JSON](api/openapi.json) and the [read-only Swagger guide](api/README.md). The client and pipeline safeguards below are not all server-side guarantees.

## Request

`GET {base_url}/availability?hotel_id=DEMO_NORTH&start_date=2026-09-27&end_date=2027-09-26`

- Header: `Authorization: Bearer <token>`, configured through `HOTEL_API_TOKEN`.
- Both range endpoints are inclusive calendar dates, not timestamps.
- `hotel_id` and room-type IDs are ASCII identifiers of 1 to 128 characters: letters, numbers, `_`, `-` and `.`, starting with a letter or digit. Display names belong in a separate dictionary.
- Follow-up pages add `cursor=<opaque string>`. A cursor is URL-encoded as a value; it is never followed as a URL.
- The base URL can contain a path prefix but not credentials, query parameters or fragments.

## HTTP 200 response

```json
{
  "items": [
    {"room_type_id": "double", "date": "2026-09-27", "available": 4}
  ],
  "next_cursor": null
}
```

`items` is an array of objects; `next_cursor` is a nonempty string (maximum 4,096 characters) or `null` for the final page. Both top-level fields are mandatory. Counts must be nonnegative 64-bit integers. Zero is meaningful; null, booleans, strings and fractional counts are errors.

Every configured room type must have one record for every requested date, including zero-inventory dates. Identical duplicates are tolerated upstream and collapsed. Conflicting duplicates, unknown rooms, out-of-range dates, missing combinations, malformed or ambiguous JSON, repeated cursors and wrong optional `hotel_id` values abort the whole snapshot before a sink write.

This strictness is intentional. If the actual vendor returns sparse inventories, omits closed room types, uses offsets, exposes a different token flow, or refreshes results during pagination, adapt the source contract explicitly. Do not silently infer missing records as zero or completeness as guaranteed.

## Resource and credential limits

Default maximums: 2 MiB per response, 1,000 pages per hotel, 100,000 source records per run (including duplicates), three attempts per request and a 10-second timeout per I/O operation. Overall job deadlines must still be configured at the scheduler or runtime level, particularly for a slow streaming server.

Only idempotent GETs retry: HTTP 408, 429, 500, 502, 503, 504 and connection errors. Authentication and authorization errors fail immediately. Backoff is bounded; a server `Retry-After` longer than 30 seconds ends the run with an error instead of retrying early. Schedule a later job retry after inspecting the failure.

All HTTP redirects are refused, including same-origin redirects. Use the canonical API endpoint, not a login page or redirector. HTTPS is required except for literal loopback addresses or `localhost` used by the fixture. Public error messages contain no token, full response body or request URL. Never enable SDK or HTTP debug logging with real credentials without reviewing its redaction behavior.

## Time semantics

- `snapshot_date` is the UTC calendar date of the collection run timestamp.
- `observed_at` is that shared UTC timestamp, not a timestamp supplied by the source.
- `stay_date` is a date-only value from the API.
- The default horizon is 365 days, not a promise about a calendar year in every leap-year and business-timezone combination.
- A newer same-day observation replaces that daily key; earlier observation days are retained. Confirm whether the buyer instead wants immutable daily observations or intraday history.

The default client bypasses environment and operating-system proxy settings for loopback URLs. Other HTTPS hosts keep normal proxy support. Each retry gets a new request object so proxy handling cannot carry a mutated tunnel target into the next attempt. An explicitly supplied opener remains the caller's responsibility.
