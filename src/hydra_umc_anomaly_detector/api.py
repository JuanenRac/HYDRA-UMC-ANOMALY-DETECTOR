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
from .drift import DriftMonitor, DriftMonitorError


MAX_BODY_BYTES = 1024 * 1024
# How much of an oversized body this drains before responding - a real,
# reproducible race found by an ecosystem-wide audit: rejecting an
# over-limit request without reading any of it left the client's own
# send() still in flight when the handler closed the connection, so on a
# body bigger than the OS socket buffer the client saw a raw
# ConnectionAbortedError instead of this clean 400 (flaky - it depended on
# how much the kernel had already buffered). Draining up to this many
# bytes lets the client finish sending before the response goes out,
# without ever holding more than one bounded read in memory.
DRAIN_CAP_BYTES = MAX_BODY_BYTES * 16


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict:
    try:
        length = int(handler.headers.get("Content-Length", 0))
    except ValueError as error:
        raise ValueError("Content-Length must be an integer") from error
    if length < 0 or length > MAX_BODY_BYTES:
        if 0 <= length <= DRAIN_CAP_BYTES:
            handler.rfile.read(length)
        raise ValueError(f"request body must contain 0-{MAX_BODY_BYTES} bytes")
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
        elif path == "/drift/init":
            self._handle_drift_init()
        elif path == "/drift/observe":
            self._handle_drift_observe()
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
        except (BaselineError, ValueError) as e:
            # BaselineError covers fit_baseline()'s own checks (too few
            # windows, mismatched lengths); the plain ValueError alongside
            # it is what compute_spectrum() raises for a bad window itself
            # (too short, not 1-D, non-numeric) - both are real client
            # input errors, not server faults, so both get the same clean
            # 400 the outer parsing errors above already produce.
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
        except (BaselineError, ValueError) as e:
            # Same reasoning as _handle_fit above: score() can raise
            # BaselineError (bin-count mismatch against the fitted
            # baseline) or a plain ValueError straight out of
            # compute_spectrum() for a bad window - both are client
            # input errors, so both become a clean 400.
            _write_json(self, 400, {"error": str(e)})
            return
        _write_json(
            self,
            200,
            {
                "score": verdict.score,
                "anomalous": verdict.anomalous,
                "worstBinFreqHz": verdict.worst_bin_freq,
                "modelVersion": verdict.model_version,
                "threshold": verdict.threshold,
            },
        )

    def _handle_drift_init(self) -> None:
        try:
            body = _read_json_body(self)
            baseline_scores = body["baselineScores"]
            if not isinstance(baseline_scores, list) or not baseline_scores:
                raise ValueError("\"baselineScores\" must be a non-empty array of numbers")
            kwargs = {}
            if "windowSize" in body:
                kwargs["window_size"] = int(body["windowSize"])
            if "driftRatioThreshold" in body:
                kwargs["drift_ratio_threshold"] = float(body["driftRatioThreshold"])
            monitor = DriftMonitor([float(s) for s in baseline_scores], **kwargs)
        except (KeyError, ValueError, TypeError, json.JSONDecodeError, DriftMonitorError) as e:
            _write_json(self, 400, {"error": f"invalid drift monitor request: {e}"})
            return
        with self.server.lock:
            self.server.drift_monitor = monitor
        _write_json(self, 200, {"status": "initialized", "baselineMeanScore": monitor.baseline_mean_score})

    def _handle_drift_observe(self) -> None:
        try:
            body = _read_json_body(self)
            score = float(body["score"])
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as e:
            _write_json(self, 400, {"error": f"invalid request: {e}"})
            return
        with self.server.lock:
            monitor = self.server.drift_monitor
            if monitor is None:
                _write_json(self, 409, {"error": "drift monitor not initialized - call POST /drift/init first"})
                return
            try:
                report = monitor.observe(score)
            except DriftMonitorError as e:
                # observe() didn't used to raise (any float, including
                # NaN/Infinity, was silently accepted) - now that it
                # rejects a non-finite score, this needs its own real
                # 400, not an uncaught exception in the request thread.
                _write_json(self, 400, {"error": f"invalid request: {e}"})
                return
        if report is None:
            _write_json(self, 200, {"status": "priming"})
            return
        _write_json(
            self,
            200,
            {
                "status": "ready",
                "baselineMeanScore": report.baseline_mean_score,
                "recentMeanScore": report.recent_mean_score,
                "driftRatio": report.drift_ratio,
                "drifted": report.drifted,
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
        self.drift_monitor: DriftMonitor | None = None
        self.lock = threading.Lock()
