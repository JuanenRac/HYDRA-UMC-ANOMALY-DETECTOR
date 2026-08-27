# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - src/hydra_umc_anomaly_detector/api.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""Plain JSON/HTTP surface (stdlib ``http.server``) over AnomalyDetector -
same convention as HYDRA-UMC-DATALAKE's own api.py in this same family.
Two real operations: calibrate the detector against known-healthy
windows, then score live windows against it.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .baseline import BaselineError
from .detector import AnomalyDetector, NotFittedError


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", 0))
    raw = handler.rfile.read(length) if length else b"{}"
    return json.loads(raw)


def _write_json(handler: BaseHTTPRequestHandler, status: int, payload: object) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    server: "DetectorServer"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # quiet by default, same reasoning as HYDRA-UMC-DATALAKE's api.py

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/baseline/fit":
            self._handle_fit()
        elif path == "/detect":
            self._handle_detect()
        else:
            _write_json(self, 404, {"error": "not found"})

    def _handle_fit(self) -> None:
        try:
            body = _read_json_body(self)
            windows = body["windows"]
            if not isinstance(windows, list) or not windows:
                raise ValueError("\"windows\" must be a non-empty array of arrays")
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as e:
            _write_json(self, 400, {"error": f"invalid request: {e}"})
            return
        try:
            with self.server.lock:
                self.server.detector.fit(windows)
        except BaselineError as e:
            _write_json(self, 400, {"error": str(e)})
            return
        _write_json(self, 200, {"status": "fitted", "windowCount": len(windows)})

    def _handle_detect(self) -> None:
        try:
            body = _read_json_body(self)
            window = body["window"]
            if not isinstance(window, list) or not window:
                raise ValueError("\"window\" must be a non-empty array")
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as e:
            _write_json(self, 400, {"error": f"invalid request: {e}"})
            return
        try:
            with self.server.lock:
                verdict = self.server.detector.score(window)
        except NotFittedError as e:
            _write_json(self, 409, {"error": str(e)})
            return
        except BaselineError as e:
            _write_json(self, 400, {"error": str(e)})
            return
        _write_json(
            self,
            200,
            {
                "score": verdict.score,
                "anomalous": verdict.anomalous,
                "worstBinFreqHz": verdict.worst_bin_freq,
            },
        )

    def do_GET(self) -> None:  # noqa: N802
        if urlparse(self.path).path == "/stats":
            with self.server.lock:
                fitted = self.server.detector.is_fitted
            _write_json(self, 200, {"fitted": fitted})
        else:
            _write_json(self, 404, {"error": "not found"})


class DetectorServer(ThreadingHTTPServer):
    """Carries one AnomalyDetector, guarded by a real Lock: fit()/score()
    mutate/read numpy state that (like HYDRA-UMC-DATALAKE's sqlite3
    connection) is not safe to touch from multiple threads at once under
    ThreadingHTTPServer without one."""

    def __init__(self, address: tuple[str, int], detector: AnomalyDetector) -> None:
        super().__init__(address, Handler)
        self.detector = detector
        self.lock = threading.Lock()
