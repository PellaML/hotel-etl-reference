"""Required offline OpenAPI validation and regressions against the real loopback fixture."""

from __future__ import annotations

import base64
import json
import re
import socket
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, timedelta
from email.message import Message
from html.parser import HTMLParser
from http.client import HTTPConnection
from pathlib import Path
from typing import Any, NoReturn
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

import pytest
from jsonschema.exceptions import ValidationError as SchemaValidationError
from jsonschema_path import SchemaPath
from openapi_schema_validator import OAS31Validator, oas31_format_checker
from openapi_spec_validator import OpenAPIV31SpecValidator
from openapi_spec_validator.validation.exceptions import OpenAPIValidationError
from referencing import Registry
from referencing.exceptions import NoSuchResource, Unresolvable
from referencing.jsonschema import DRAFT202012

from hotel_etl.api import HttpAvailabilityClient
from hotel_etl.errors import SourceError
from hotel_etl.fixtures import DEMO_HOTELS, DEMO_TOKEN, fixture_api
from hotel_etl.models import MAX_INT64, Hotel

DOCS = Path(__file__).resolve().parents[1] / "docs" / "api"
OBSERVATION = date(2026, 9, 27)
BASE_QUERY = {
    "hotel_id": "DEMO_NORTH",
    "start_date": "2026-09-27",
    "end_date": "2026-09-28",
}
AUTHORIZATION = f"Bearer {DEMO_TOKEN}"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    assert len(result) == len(pairs), "Duplicate JSON object keys in documentation"
    return result


def _reject_constant(value: str) -> None:
    raise AssertionError(f"Non-JSON constant in documentation: {value}")


def _read_json(name: str) -> dict[str, Any]:
    """Check documentation JSON independently of the application input decoder."""
    return json.loads(
        (DOCS / name).read_text(encoding="utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )


SPEC = _read_json("openapi.json")
OPERATION = SPEC["paths"]["/availability"]["get"]


def _resolve(reference: str) -> dict[str, Any]:
    assert reference.startswith("#/"), "Only local references are allowed"
    node = SPEC
    for token in reference[2:].split("/"):
        node = node[token.replace("~1", "/").replace("~0", "~")]
    assert isinstance(node, dict)
    return node


def _walk(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _require_local_references(document: Any) -> None:
    for node in _walk(document):
        if "$ref" in node:
            reference = node["$ref"]
            assert isinstance(reference, str) and reference.startswith("#/"), (
                "Only document-local references are allowed"
            )
        assert "externalValue" not in node, "External example resources are forbidden"
        assert "$dynamicRef" not in node, "Dynamic references are not used by this contract"


def _deny_retrieval(uri: str) -> NoReturn:
    raise NoSuchResource(ref=uri)


_SCHEMA_VALIDATOR = OAS31Validator(
    SPEC,
    format_checker=oas31_format_checker,
    registry=Registry(retrieve=_deny_retrieval),
)


def _check_schema(value: Any, schema: dict[str, Any]) -> None:
    _require_local_references(schema)
    # Keep the document's resolver when checking a subschema with #/components references.
    _SCHEMA_VALIDATOR.evolve(schema=schema).validate(value)


def _validate_document(document: dict[str, Any]) -> None:
    _require_local_references(document)
    # An empty handler map disables HTTP, file and catch-all URL retrieval.
    OpenAPIV31SpecValidator(SchemaPath.from_dict(document, handlers={})).validate()


def _schema_nodes(schema: Any) -> Iterator[dict[str, Any]]:
    if isinstance(schema, dict):
        yield schema
    for resource in DRAFT202012.create_resource(schema).subresources():
        yield from _schema_nodes(resource.contents)


@pytest.fixture
def no_schema_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("Schema validation must not resolve DNS or open a connection")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


@dataclass(frozen=True)
class WireResponse:
    status: int
    headers: Message
    body: bytes


def _request(
    base_url: str,
    query: dict[str, Any] | str | None = None,
    *,
    authorization: str | None = AUTHORIZATION,
    path: str = "/availability",
    method: str = "GET",
    extra_headers: dict[str, str] | None = None,
) -> WireResponse:
    if query is None:
        query = BASE_QUERY
    encoded = query if isinstance(query, str) else urlencode(query)
    headers = dict(extra_headers or {})
    if authorization is not None:
        headers["Authorization"] = authorization
    request = Request(base_url + path + "?" + encoded, headers=headers, method=method)
    try:
        response = build_opener(ProxyHandler({})).open(request, timeout=5)
    except HTTPError as error:
        response = error
    with response:
        return WireResponse(response.status, response.headers, response.read())


def _documented_json(response: WireResponse) -> dict[str, Any]:
    assert response.headers["Content-Type"] == "application/json"
    assert int(response.headers["Content-Length"]) == len(response.body)
    assert response.headers.get("Access-Control-Allow-Origin") is None
    if response.status == 401:
        assert response.headers.get("WWW-Authenticate") is None
    payload = json.loads(response.body.decode("utf-8"))
    media = OPERATION["responses"][str(response.status)]["content"]["application/json"]
    _check_schema(payload, media["schema"])
    if response.status == 200:
        for item in payload["items"]:
            assert type(item["available"]) is int, "The fixture emits integer JSON counts"
    return payload


RESPONSE_EXAMPLES = [
    pytest.param(int(status), name, example, id=f"{status}-{name}")
    for status, response in OPERATION["responses"].items()
    for name, example in response["content"]["application/json"]["examples"].items()
]


def test_document_structure_and_local_references() -> None:
    assert SPEC["openapi"] == "3.1.0"
    assert SPEC["jsonSchemaDialect"] == "https://spec.openapis.org/oas/3.1/dialect/base"
    assert SPEC["info"]["version"] == "0.1.0"
    assert set(SPEC["paths"]) == {"/availability"}
    assert set(SPEC["paths"]["/availability"]) == {"get"}
    assert set(OPERATION["responses"]) == {"200", "400", "401"}
    assert "requestBody" not in OPERATION
    assert SPEC["security"] == [{"fixtureBearer": []}]
    scheme = SPEC["components"]["securitySchemes"]["fixtureBearer"]
    assert (scheme["type"], scheme["scheme"]) == ("http", "bearer")
    assert AUTHORIZATION in scheme["description"]
    assert SPEC["servers"][0]["url"] == "http://127.0.0.1:{port}"
    assert SPEC["servers"][0]["variables"]["port"]["default"] == "0"
    _require_local_references(SPEC)
    for node in _walk(SPEC):
        if "$ref" in node:
            _resolve(node["$ref"])


@pytest.mark.usefixtures("no_schema_network")
def test_complete_document_is_valid_openapi_31() -> None:
    _validate_document(SPEC)


@pytest.mark.usefixtures("no_schema_network")
def test_complete_validator_rejects_an_invalid_openapi_document() -> None:
    invalid = deepcopy(SPEC)
    del invalid["info"]["title"]
    with pytest.raises(OpenAPIValidationError, match="title"):
        _validate_document(invalid)


@pytest.mark.usefixtures("no_schema_network")
def test_parameter_and_schema_examples_obey_the_documented_constraints() -> None:
    parameters = [_resolve(item["$ref"]) for item in OPERATION["parameters"]]
    assert [(item["name"], item["required"]) for item in parameters] == [
        ("hotel_id", True),
        ("start_date", True),
        ("end_date", True),
        ("cursor", False),
    ]
    for parameter in parameters:
        assert parameter["in"] == "query"
        assert (parameter["style"], parameter["explode"]) == ("form", False)
        for example in parameter["examples"].values():
            _check_schema(example["value"], parameter["schema"])
    roots = list(SPEC["components"]["schemas"].values())
    roots += [item["schema"] for item in parameters]
    roots += [item["schema"] for item in SPEC["components"]["headers"].values()]
    for root in roots:
        OAS31Validator.check_schema(root)
        for schema in _schema_nodes(root):
            if "format" in schema:
                assert schema["format"] in oas31_format_checker.checkers
            for value in schema.get("examples", []):
                _check_schema(value, schema)
            if "default" in schema:
                _check_schema(schema["default"], schema)
    assert OPERATION["x-inclusive-day-limit"] == 366
    assert OPERATION["x-default-page-size"] == 128
    count = SPEC["components"]["schemas"]["AvailabilityItem"]["properties"]["available"]
    assert count["maximum"] == 11 < MAX_INT64
    assert (
        "maxItems" not in SPEC["components"]["schemas"]["AvailabilityPage"]["properties"]["items"]
    )
    cursor = SPEC["components"]["parameters"]["Cursor"]["schema"]
    assert cursor == {"type": "integer", "minimum": 0, "default": 0}


@pytest.mark.parametrize("status,name,example", RESPONSE_EXAMPLES)
def test_every_response_example_matches_an_actual_fixture_response(
    status: int, name: str, example: dict[str, Any]
) -> None:
    context = example["x-fixture-request"]
    assert set(context) == {"query", "page_size", "authorized"}
    assert type(context["page_size"]) is int and context["page_size"] > 0
    with fixture_api(DEMO_HOTELS, OBSERVATION, page_size=context["page_size"]) as base_url:
        actual = _request(
            base_url,
            context["query"],
            authorization=AUTHORIZATION if context["authorized"] else None,
        )
    assert actual.status == status, name
    assert _documented_json(actual) == example["value"], name


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "",
        "Bearer incorrect-synthetic-token",
        f"bearer {DEMO_TOKEN}",
        f"Bearer  {DEMO_TOKEN}",
        f"Bearer {DEMO_TOKEN} ",
    ],
)
@pytest.mark.parametrize(
    "path,query", [("/availability", BASE_QUERY), ("/missing", ""), ("/availability", "cursor=bad")]
)
def test_authentication_precedes_get_path_and_query_validation(
    authorization: str | None, path: str, query: dict[str, Any] | str
) -> None:
    with fixture_api(DEMO_HOTELS, OBSERVATION) as base_url:
        response = _request(base_url, query, authorization=authorization, path=path)
    assert response.status == 401
    assert _documented_json(response) == {"error": "Unauthorized"}


@pytest.mark.parametrize("first_is_valid", [True, False])
def test_header_names_are_case_insensitive_and_first_authorization_value_wins(
    first_is_valid: bool,
) -> None:
    with fixture_api(DEMO_HOTELS, OBSERVATION) as base_url:
        target = urlsplit(base_url)
        connection = HTTPConnection(target.hostname, target.port, timeout=5)
        try:
            connection.putrequest("GET", "/availability?" + urlencode(BASE_QUERY))
            values = [AUTHORIZATION, "Bearer wrong-synthetic-token"]
            if not first_is_valid:
                values.reverse()
            for value in values:
                connection.putheader("aUtHoRiZaTiOn", value)
            connection.endheaders()
            with connection.getresponse() as response:
                result = WireResponse(response.status, response.headers, response.read())
        finally:
            connection.close()
    assert result.status == (200 if first_is_valid else 401)
    _documented_json(result)


INVALID_QUERIES = [
    "",
    urlencode({key: value for key, value in BASE_QUERY.items() if key != "hotel_id"}),
    urlencode({**BASE_QUERY, "hotel_id": ""}),
    urlencode({**BASE_QUERY, "hotel_id": "NOT_CONFIGURED"}),
    urlencode({**BASE_QUERY, "hotel_id": "bad hotel"}),
    urlencode({**BASE_QUERY, "start_date": ""}),
    urlencode({**BASE_QUERY, "end_date": ""}),
    urlencode({**BASE_QUERY, "start_date": "2026-02-29"}),
    urlencode({**BASE_QUERY, "start_date": "2026-09-27T00:00:00"}),
    urlencode({**BASE_QUERY, "start_date": "2026-9-27"}),
    urlencode({**BASE_QUERY, "start_date": "2026-W54-1"}),
    urlencode({**BASE_QUERY, "end_date": "2026-09-26"}),
    urlencode({**BASE_QUERY, "end_date": "2027-09-28"}),
    urlencode({**BASE_QUERY, "cursor": -1}),
    urlencode({**BASE_QUERY, "cursor": "opaque-not-an-offset"}),
    urlencode({**BASE_QUERY, "cursor": "1.5"}),
    urlencode(BASE_QUERY) + "&hotel_id=DEMO_NORTH",
    urlencode(BASE_QUERY) + "&cursor=0&cursor=0",
    urlencode(BASE_QUERY) + "&ignored=a&ignored=b",
    urlencode(BASE_QUERY) + "&bare-field",
    urlencode(BASE_QUERY) + "&&ignored=one",
]


@pytest.mark.parametrize("query", INVALID_QUERIES)
def test_invalid_query_errors_are_documented(query: str) -> None:
    with fixture_api(DEMO_HOTELS, OBSERVATION) as base_url:
        response = _request(base_url, query)
    assert response.status == 400
    assert _documented_json(response) == {"error": "Invalid fixture request"}


@pytest.mark.parametrize("path", ["/availability/", "/v1/availability", "/missing"])
def test_unknown_authenticated_get_path_is_400_not_404(path: str) -> None:
    with fixture_api(DEMO_HOTELS, OBSERVATION) as base_url:
        response = _request(base_url, path=path)
    assert response.status == 400
    assert _documented_json(response) == {"error": "Invalid fixture request"}


@pytest.mark.parametrize(
    "suffix",
    [
        "&cursor=",
        "&cursor=&cursor=0",
        "&hotel_id=",
        "&ignored=a",
        "&ignored=",
        "&ignored=&ignored=a",
        "&page_size=1",
    ],
)
def test_blank_values_and_single_unknown_parameters_follow_parse_qs(suffix: str) -> None:
    with fixture_api(DEMO_HOTELS, OBSERVATION) as base_url:
        response = _request(base_url, urlencode(BASE_QUERY) + suffix)
    assert response.status == 200
    payload = _documented_json(response)
    assert len(payload["items"]) == 6
    assert payload["next_cursor"] is None


@pytest.mark.parametrize("cursor", ["00", "+0", " 0 ", "0_0", "-0", "\u0660"])
def test_cursor_int_parser_is_more_permissive_than_canonical_digits(cursor: str) -> None:
    with fixture_api(DEMO_HOTELS, OBSERVATION) as base_url:
        response = _request(base_url, {**BASE_QUERY, "cursor": cursor})
    assert response.status == 200
    assert len(_documented_json(response)["items"]) == 6


@pytest.mark.parametrize(
    "wire_date,expected",
    [
        ("2026-09-27", "2026-09-27"),
        ("20260927", "2026-09-27"),
        ("2026-W39-7", "2026-09-27"),
        ("2026W397", "2026-09-27"),
        ("2026-W39", "2026-09-21"),
        ("2026W39", "2026-09-21"),
        ("2028-02-29", "2028-02-29"),
        ("0001-01-01", "0001-01-01"),
        ("9999-12-31", "9999-12-31"),
    ],
)
def test_actual_date_spellings_match_request_schema_and_canonical_responses(
    wire_date: str, expected: str
) -> None:
    _check_schema(wire_date, SPEC["components"]["schemas"]["FixtureDateInput"])
    with fixture_api(DEMO_HOTELS, OBSERVATION) as base_url:
        response = _request(
            base_url, {**BASE_QUERY, "start_date": wire_date, "end_date": wire_date}
        )
    assert response.status == 200
    payload = _documented_json(response)
    assert len(payload["items"]) == 3
    assert {item["date"] for item in payload["items"]} == {expected}


def test_the_4096_character_cursor_limit_belongs_to_the_client_not_the_fixture() -> None:
    cursor = " " * 4096 + "0"
    with fixture_api(DEMO_HOTELS, OBSERVATION) as base_url:
        response = _request(base_url, {**BASE_QUERY, "cursor": cursor})
    assert response.status == 200
    assert len(_documented_json(response)["items"]) == 6


def test_cursor_upper_bound_scales_with_configured_rooms_not_a_fixed_1098() -> None:
    hotel = Hotel("MORE_ROOMS", ("a", "b", "c", "d"))
    query = {**BASE_QUERY, "hotel_id": hotel.hotel_id, "end_date": "2027-09-27"}
    with fixture_api((hotel,), OBSERVATION) as base_url:
        response = _request(base_url, {**query, "cursor": 1099})
        final = _request(base_url, {**query, "cursor": 1464})
    assert response.status == final.status == 200
    page = _documented_json(response)
    assert len(page["items"]) == 128
    assert page["next_cursor"] == "1227"
    assert _documented_json(final) == {"items": [], "next_cursor": None}


def test_default_page_size_maximum_range_and_cursor_boundaries() -> None:
    query = {**BASE_QUERY, "end_date": "2027-09-27"}
    with fixture_api(DEMO_HOTELS, OBSERVATION) as base_url:
        first = _request(base_url, query)
        assert first.status == 200
        page = _documented_json(first)
        assert len(page["items"]) == 128 and page["next_cursor"] == "128"
        at_end = _request(base_url, {**query, "cursor": 1098})
        beyond = _request(base_url, {**query, "cursor": 1099})
        too_long = _request(base_url, {**query, "end_date": "2027-09-28"})
    assert at_end.status == 200
    assert _documented_json(at_end) == {"items": [], "next_cursor": None}
    assert beyond.status == too_long.status == 400
    assert _documented_json(beyond) == {"error": "Invalid cursor"}
    assert _documented_json(too_long) == {"error": "Invalid fixture request"}


def test_pagination_is_complete_ordered_and_accepted_by_the_existing_adapter() -> None:
    hotel = Hotel("Custom.Hotel-7", ("z-room", "a_room"))
    end = OBSERVATION + timedelta(days=11)
    query = {
        "hotel_id": hotel.hotel_id,
        "start_date": OBSERVATION.isoformat(),
        "end_date": end.isoformat(),
    }
    rows: list[dict[str, Any]] = []
    cursors: list[str] = []
    with fixture_api((hotel,), OBSERVATION, page_size=5) as base_url:
        for _ in range(5):
            response = _request(base_url, query)
            assert response.status == 200
            payload = _documented_json(response)
            rows.extend(payload["items"])
            if payload["next_cursor"] is None:
                break
            cursors.append(payload["next_cursor"])
            query["cursor"] = payload["next_cursor"]
        else:
            pytest.fail("Expected a terminal page in five requests")
        client_rows = list(
            HttpAvailabilityClient(base_url, DEMO_TOKEN).fetch_availability(
                hotel.hotel_id,
                OBSERVATION,
                end,
            )
        )
    expected = [
        {
            "room_type_id": room,
            "date": day.isoformat(),
            "available": (
                sum(map(ord, hotel.hotel_id + room)) + OBSERVATION.toordinal() + day.toordinal()
            )
            % 12,
        }
        for room in hotel.room_type_ids
        for day in (OBSERVATION + timedelta(days=offset) for offset in range(12))
    ]
    assert rows == client_rows == expected
    assert cursors == ["5", "10", "15", "20"]
    assert {item["available"] for item in rows} == set(range(12))


def test_maximum_length_custom_identifiers_are_supported() -> None:
    hotel = Hotel("H" * 128, ("R" * 128,))
    with fixture_api((hotel,), OBSERVATION) as base_url:
        response = _request(base_url, {**BASE_QUERY, "hotel_id": hotel.hotel_id})
    assert response.status == 200
    assert {item["room_type_id"] for item in _documented_json(response)["items"]} == {"R" * 128}


def test_accept_is_not_negotiated_and_no_cors_headers_are_added() -> None:
    with fixture_api(DEMO_HOTELS, OBSERVATION) as base_url:
        response = _request(base_url, extra_headers={"Accept": "text/plain", "Origin": "null"})
    assert response.status == 200
    _documented_json(response)


@pytest.mark.parametrize("method", ["HEAD", "POST", "OPTIONS"])
@pytest.mark.parametrize("authorization", [None, "Bearer wrong-synthetic-token"])
def test_unsupported_methods_use_stdlib_501_not_get_authentication(
    method: str, authorization: str | None
) -> None:
    with fixture_api(DEMO_HOTELS, OBSERVATION) as base_url:
        response = _request(base_url, authorization=authorization, method=method)
    assert response.status == 501
    assert response.headers.get_content_type() == "text/html"
    assert response.headers.get("Access-Control-Allow-Origin") is None
    assert response.headers.get("WWW-Authenticate") is None
    if method == "HEAD":
        assert response.body == b""
    else:
        assert b"Unsupported method" in response.body
        assert int(response.headers["Content-Length"]) == len(response.body)


def test_zero_page_size_is_not_validated_by_fixture_but_client_refuses_repeated_cursor() -> None:
    with fixture_api(DEMO_HOTELS, OBSERVATION, page_size=0) as base_url:
        response = _request(base_url)
        assert response.status == 200
        assert _documented_json(response) == {"items": [], "next_cursor": "0"}
        client = HttpAvailabilityClient(base_url, DEMO_TOKEN, max_pages=3)
        with pytest.raises(SourceError, match="cursor repeated"):
            list(client.fetch_availability("DEMO_NORTH", OBSERVATION, OBSERVATION))


@pytest.mark.parametrize("identifier", ["", "a" * 129, "_room", "room name", "room\n", "\u0142"])
def test_identifier_schema_rejects_non_ids(identifier: str) -> None:
    with pytest.raises(SchemaValidationError):
        _check_schema(identifier, SPEC["components"]["schemas"]["Identifier"])


@pytest.mark.parametrize("count", [True, -1, 12, 1.5, "1", None, MAX_INT64])
def test_fixture_count_schema_does_not_claim_generic_adapter_range(count: object) -> None:
    item = {"room_type_id": "single", "date": "2026-09-27", "available": count}
    with pytest.raises(SchemaValidationError):
        _check_schema(item, SPEC["components"]["schemas"]["AvailabilityItem"])


@pytest.mark.parametrize(
    "payload",
    [
        {"items": []},
        {"next_cursor": None},
        {"items": [], "next_cursor": ""},
        {"items": [], "next_cursor": False},
        {"items": [], "next_cursor": "-1"},
        {"items": [], "next_cursor": None, "extra": 1},
    ],
)
def test_page_schema_does_not_accept_invalid_fixture_shapes(payload: dict[str, Any]) -> None:
    with pytest.raises(SchemaValidationError):
        _check_schema(payload, SPEC["components"]["schemas"]["AvailabilityPage"])


def test_json_schema_integer_semantics_are_separate_from_fixture_wire_types() -> None:
    payload = {
        "items": [{"room_type_id": "single", "date": "2026-09-27", "available": 1.0}],
        "next_cursor": None,
    }
    # JSON Schema permits 1.0 as an integer; the fixture actually serializes Python ints.
    _check_schema(payload, SPEC["components"]["schemas"]["AvailabilityPage"])
    body = json.dumps(payload).encode("utf-8")
    headers = Message()
    headers["Content-Type"] = "application/json"
    headers["Content-Length"] = str(len(body))
    with pytest.raises(AssertionError, match="fixture emits integer JSON counts"):
        _documented_json(WireResponse(200, headers, body))


@pytest.mark.usefixtures("no_schema_network")
def test_schema_rejects_unevaluated_properties() -> None:
    schema = {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "unevaluatedProperties": False,
    }
    _check_schema({"count": 1}, schema)
    with pytest.raises(SchemaValidationError, match="Unevaluated properties"):
        _check_schema({"count": 1, "extra": 2}, schema)


@pytest.mark.usefixtures("no_schema_network")
@pytest.mark.parametrize("value", ["2026-02-29", "2026-9-27", "20260927", "2026-09-27T00:00:00"])
def test_format_checking_is_required_without_a_pattern_to_mask_it(value: str) -> None:
    schema = {"type": "string", "format": "date"}
    _check_schema("2028-02-29", schema)
    with pytest.raises(SchemaValidationError, match="date"):
        _check_schema(value, schema)


NONLOCAL_REFERENCES = [
    "https://example.invalid/schema.json",
    "http://127.0.0.1:9/schema.json",
    "file:///not-allowed.json",
    "other.json#/definitions/value",
]


@pytest.mark.usefixtures("no_schema_network")
@pytest.mark.parametrize("reference", NONLOCAL_REFERENCES)
def test_reference_policy_rejects_nonlocal_resources_before_validation(reference: str) -> None:
    invalid = deepcopy(SPEC)
    invalid["paths"]["/availability"]["get"]["parameters"][0] = {"$ref": reference}
    with pytest.raises(AssertionError, match="document-local"):
        _validate_document(invalid)
    with pytest.raises(AssertionError, match="document-local"):
        _check_schema(None, {"$ref": reference})


@pytest.mark.usefixtures("no_schema_network")
@pytest.mark.parametrize("reference", NONLOCAL_REFERENCES)
def test_retrievers_stay_disabled_even_without_the_policy_precheck(reference: str) -> None:
    with pytest.raises(Unresolvable):
        _SCHEMA_VALIDATOR.evolve(schema={"$ref": reference}).validate(None)
    invalid = deepcopy(SPEC)
    invalid["paths"]["/availability"]["get"]["parameters"][0] = {"$ref": reference}
    with pytest.raises(Unresolvable):
        OpenAPIV31SpecValidator(SchemaPath.from_dict(invalid, handlers={})).validate()


@pytest.mark.usefixtures("no_schema_network")
@pytest.mark.parametrize("keyword", ["externalValue", "$dynamicRef"])
def test_external_examples_and_dynamic_references_remain_forbidden(keyword: str) -> None:
    invalid = deepcopy(SPEC)
    invalid[keyword] = "https://example.invalid/unused"
    with pytest.raises(AssertionError, match="External example|Dynamic references"):
        _validate_document(invalid)


class Markup(HTMLParser):
    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.feed(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))


def test_viewer_has_an_early_strict_csp_and_no_automatic_remote_assets() -> None:
    markup = Markup((DOCS / "index.html").read_text(encoding="utf-8"))
    policy_index, policy = next(
        (index, attrs["content"])
        for index, (tag, attrs) in enumerate(markup.tags)
        if tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy"
    )
    assert policy is not None
    directives = {parts[0]: parts[1:] for item in policy.split(";") if (parts := item.split())}
    assert directives["default-src"] == ["'none'"]
    assert directives["connect-src"] == ["'self'"]
    assert directives["script-src-attr"] == directives["style-src-attr"] == ["'none'"]
    for directive in ("form-action", "frame-src", "worker-src", "object-src", "base-uri"):
        assert directives[directive] == ["'none'"]
    assert not {"frame-ancestors", "sandbox", "report-uri"} & directives.keys()
    assert "unsafe-inline" not in policy and "unsafe-eval" not in policy
    assert "*" not in policy
    manifest = _read_json("swagger-ui-assets.json")
    assets = {item["kind"]: item for item in manifest["assets"]}
    assert directives["script-src"] == ["'self'", assets["script"]["url"]]
    assert directives["style-src"] == ["'self'", assets["style"]["url"]]
    for index, (tag, attrs) in enumerate(markup.tags):
        assert not any(key.startswith("on") for key in attrs)
        if tag in {"script", "link"}:
            assert index > policy_index
        if tag == "script":
            assert attrs["src"] == "viewer.js" and "defer" in attrs
        if tag == "link":
            assert attrs["href"] in {"viewer.css", "data:,"}
        assert tag not in {"iframe", "form", "input"}
    assert any(
        tag == "meta" and attrs == {"name": "referrer", "content": "no-referrer"}
        for tag, attrs in markup.tags
    )


def test_viewer_uses_fixed_pinned_assets_matching_the_provenance_record() -> None:
    script = (DOCS / "viewer.js").read_text(encoding="utf-8")
    matched = re.search(r"const ASSETS = Object\.freeze\((\[[\s\S]*?\])\);", script)
    assert matched is not None
    fixed_assets = json.loads(matched[1])
    manifest = _read_json("swagger-ui-assets.json")
    assert manifest["version"] == "5.33.0"
    assert manifest["license"]["spdx"] == "Apache-2.0"
    assert len(fixed_assets) == len(manifest["assets"]) == 2
    notes = (DOCS / "THIRD_PARTY.md").read_text(encoding="utf-8")
    for fixed, recorded in zip(fixed_assets, manifest["assets"], strict=True):
        assert fixed == {key: recorded[key] for key in ("kind", "url", "integrity")}
        assert recorded["url"] == (
            f"https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.33.0/{recorded['file']}"
        )
        assert recorded["source"] == (
            f"https://raw.githubusercontent.com/swagger-api/swagger-ui/v5.33.0/dist/{recorded['file']}"
        )
        assert recorded["integrity"].startswith("sha384-")
        assert len(base64.b64decode(recorded["integrity"][7:], validate=True)) == 48
        assert recorded["integrity"] in notes
        assert recorded["bytes"] > 0
    for evidence in [manifest["license"], *manifest["notices"]]:
        assert len(base64.b64decode(evidence["sha384"], validate=True)) == 48
        assert evidence["source"] in notes


def test_viewer_disables_requests_credentials_and_arbitrary_spec_configuration() -> None:
    script = (DOCS / "viewer.js").read_text(encoding="utf-8")
    for setting in (
        "supportedSubmitMethods: []",
        "tryItOutEnabled: false",
        "validatorUrl: null",
        "persistAuthorization: false",
        "withCredentials: false",
        "queryConfigEnabled: false",
        "useUnsafeMarkdown: false",
        'credentials: "omit"',
        'redirect: "error"',
        'mode: "same-origin"',
        'element.crossOrigin = "anonymous"',
        'element.referrerPolicy = "no-referrer"',
        "element.integrity = asset.integrity",
    ):
        assert setting in script
    assert re.search(r"requestInterceptor:\s*\(\) =>\s*\{\s*throw new Error", script)
    for component in ("authorizeBtn", "authorizeOperationBtn", "authorizationPopup"):
        assert f"{component}: () => null" in script
    assert 'new URL("openapi.json", location.href)' in script
    assert "requireLocalReferences(spec)" in script
    assert len(re.findall(r"\bfetch\(", script)) == 1
    for forbidden in (
        "URLSearchParams",
        "location.search",
        "location.hash",
        "configUrl:",
        "url:",
        "localStorage",
        "sessionStorage",
        "indexedDB",
        "document.cookie",
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
    ):
        assert forbidden not in script


def test_viewer_styles_do_not_override_swagger_buttons_or_version_badges() -> None:
    stylesheet = (DOCS / "viewer.css").read_text(encoding="utf-8")
    assert "#spec-json {" in stylesheet
    assert "#load-swagger {" in stylesheet
    assert not re.search(r"^\s*(?:pre|button)(?:[\s:{,.#]|$)", stylesheet, re.MULTILINE)


def test_documented_validation_setup_preserves_cloud_dependencies() -> None:
    guide = (DOCS / "README.md").read_text(encoding="utf-8")
    commands = re.findall(r"^uv (?:sync|run) .+$", guide, re.MULTILINE)
    assert commands
    assert all("--all-extras" in command for command in commands)
