"""Synthetic data and a loopback-only HTTP fixture, never a real hotel integration."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from hotel_etl.models import Hotel

DEMO_TOKEN = "synthetic-demo-token-not-a-secret"
DEMO_HOTELS = (
    Hotel("DEMO_NORTH", ("single", "double", "suite")),
    Hotel("DEMO_SOUTH", ("single", "double", "suite")),
)


def synthetic_items(
    hotel: Hotel, start_date: date, end_date: date, observation_date: date
) -> list[dict[str, object]]:
    """Reproducible counts; no personal data, names, booking IDs or real inventories."""
    items: list[dict[str, object]] = []
    for room in hotel.room_type_ids:
        seed = sum(map(ord, hotel.hotel_id + room)) + observation_date.toordinal()
        for day in range((end_date - start_date).days + 1):
            stay = start_date + timedelta(days=day)
            items.append(
                {
                    "room_type_id": room,
                    "date": stay.isoformat(),
                    "available": (seed + stay.toordinal()) % 12,
                }
            )
    return items


@contextmanager
def fixture_api(
    hotels: Sequence[Hotel], observation_date: date, *, page_size: int = 128
) -> Iterator[str]:
    by_id = {hotel.hotel_id: hotel for hotel in hotels}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *args: object) -> None:
            # No request URLs or headers in test/demo logs.
            pass

        def send_json(self, status: int, body: dict[str, object]) -> None:
            encoded = json.dumps(body, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:
            if self.headers.get("Authorization") != f"Bearer {DEMO_TOKEN}":
                self.send_json(401, {"error": "Unauthorized"})
                return
            parsed = urlsplit(self.path)
            try:
                if parsed.path != "/availability":
                    raise ValueError
                params = parse_qs(parsed.query, strict_parsing=True)
                if any(len(values) != 1 for values in params.values()):
                    raise ValueError
                hotel = by_id[params["hotel_id"][0]]
                start = date.fromisoformat(params["start_date"][0])
                end = date.fromisoformat(params["end_date"][0])
                cursor = int(params.get("cursor", ["0"])[0])
                if cursor < 0 or end < start or (end - start).days > 365:
                    raise ValueError
            except (KeyError, ValueError):
                self.send_json(400, {"error": "Invalid fixture request"})
                return
            items = synthetic_items(hotel, start, end, observation_date)
            if cursor > len(items):
                self.send_json(400, {"error": "Invalid cursor"})
                return
            stop = cursor + page_size
            self.send_json(
                200,
                {
                    "items": items[cursor:stop],
                    "next_cursor": str(stop) if stop < len(items) else None,
                },
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
