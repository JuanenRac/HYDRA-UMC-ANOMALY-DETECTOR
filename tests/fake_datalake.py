# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - tests/fake_datalake.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""A real HTTP server implementing HYDRA-UMC-DATALAKE's actual GET
/query response contract (sourceId/kind/field/timestamp/value JSON
objects) - used by this project's own tests to prove datalake_client.py
really parses and uses what a real DATALAKE would send back, over a real
socket, not a mocked function call. Same fixture as
HYDRA-UMC-PRODUCTION-REPORTS' own tests/fake_datalake.py - one real
DATALAKE contract, reused verbatim rather than reinvented per consumer.
"""
from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class _Handler(BaseHTTPRequestHandler):
    server: "FakeDatalakeServer"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/ingest":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.ingested.append({"body": body, "authorization": self.headers.get("Authorization")})
        if self.server.fail_ingest:
            self.send_response(503)
            self.end_headers()
            return
        reply = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        if parsed.path == "/query":
            self.server.query_authorizations.append(self.headers.get("Authorization"))
            points = self.server.points
            if "sourceId" in params:
                points = [p for p in points if p["sourceId"] == params["sourceId"]]
            if "kind" in params:
                points = [p for p in points if p["kind"] == params["kind"]]
            if "field" in params:
                points = [p for p in points if p["field"] == params["field"]]
            if "start" in params:
                points = [p for p in points if p["timestamp"] >= int(params["start"])]
            if "end" in params:
                points = [p for p in points if p["timestamp"] <= int(params["end"])]
            points = sorted(points, key=lambda p: p["timestamp"])
            if "limit" in params:
                points = points[: int(params["limit"])]
            body = json.dumps(points).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/error":
            self.send_response(500)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()


class FakeDatalakeServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, _Handler)
        self.points: list[dict] = []
        self.ingested: list[dict] = []  # what POST /ingest received, with the Authorization header it came with
        self.query_authorizations: list[str | None] = []
        self.fail_ingest = False


@contextmanager
def running_fake_datalake() -> Iterator[tuple[str, FakeDatalakeServer]]:
    server = FakeDatalakeServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
