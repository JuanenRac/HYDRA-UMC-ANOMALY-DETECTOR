# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - src/hydra_umc_anomaly_detector/api.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Plain JSON/HTTP surface (stdlib ``http.server``) over AnomalyDetector -
same convention as HYDRA-UMC-DATALAKE's own api.py in this same family.
Two real operations: calibrate the detector against known-healthy
windows, then score live windows against it.
"""
from __future__ import annotations

import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .baseline import BaselineError, save_baseline
from .detector import AnomalyDetector, NotFittedError
from .drift import DriftMonitor, DriftMonitorError


MAX_BODY_BYTES = 1024 * 1024
# How much of an oversized body this drains before responding - a real,
# reproducible race: rejecting an over-limit request without reading any
# of it left the client's own
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
    return json.loads(raw, parse_constant=_reject_json_constant)


def _reject_json_constant(name: str) -> float:
    # json.loads would otherwise accept NaN / Infinity / -Infinity
    raise ValueError(f"{name} is not a valid JSON number")


def _authorized(handler: BaseHTTPRequestHandler) -> bool:
    """Operations that change the model or the drift monitor need the bearer token when the server has one."""
    expected = handler.server.token
    if not expected:
        return True
    scheme, _, presented = handler.headers.get("Authorization", "").partition(" ")
    return scheme == "Bearer" and hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


# the POST routes that change state; /detect only reads the model
_WRITE_ROUTES = ("/baseline/fit", "/drift/init", "/drift/observe")


def _write_json(handler: BaseHTTPRequestHandler, status: int, payload: object) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    server: "DetectorServer"

    # Seconds a client may stay silent on its connection before the server
    # drops it: with no limit, a client that opens a connection and sends
    # nothing (or a body one byte at a time) holds a handler thread forever.
    # It bounds socket I/O only, never the time a request spends computing.
    timeout = 15.0

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # quiet by default, same reasoning as HYDRA-UMC-DATALAKE's api.py

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if (path in _WRITE_ROUTES or (path == "/detect" and self.server.protect_reads)) and not _authorized(self):
            _write_json(self, 401, {"error": "a valid bearer token is required for this operation"})
            return
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
            # the FFTs run outside the lock, so a big fit never blocks /detect;
            # only swapping the model in (and saving it) happens inside it
            prepared = self.server.detector.prepare_fit(windows)
            with self.server.lock:
                self.server.detector.adopt(prepared)
                if self.server.baseline_path is not None:
                    # The fitted baseline used to live only in memory -
                    # persist it now, inside the
                    # same lock, so a concurrent GET /stats or POST
                    # /detect never observes a fit that hasn't actually
                    # been saved yet.
                    save_baseline(self.server.baseline_path, self.server.detector.baseline, self.server.detector.model_version)
        except (BaselineError, ValueError, TypeError, OverflowError) as e:
            # BaselineError covers fit_baseline()'s own checks (too few
            # windows, mismatched lengths); the plain ValueError alongside
            # it is what compute_spectrum() raises for a bad window itself
            # (too short, not 1-D, non-numeric) - both are real client
            # input errors, not server faults, so both get the same clean
            # 400 the outer parsing errors above already produce.
            _write_json(self, 400, {"error": str(e)})
            return
        except OSError as e:
            # A successful fit that couldn't be persisted (disk full,
            # permission denied) is a real server-side problem, not a
            # malformed request - the in-memory model is still fitted
            # and usable, so this is reported distinctly rather than as
            # a 400 that would wrongly suggest retrying the same request
            # differently would help.
            _write_json(self, 500, {"error": f"fitted but could not persist baseline: {e}"})
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
        except (BaselineError, ValueError, TypeError, OverflowError) as e:
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
                "calibrationWindows": verdict.calibration_windows,
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
        except (KeyError, ValueError, TypeError, OverflowError, json.JSONDecodeError, DriftMonitorError) as e:
            _write_json(self, 400, {"error": f"invalid drift monitor request: {e}"})
            return
        with self.server.lock:
            self.server.drift_monitor = monitor
            # the reference scores belong to the model that exists right now
            self.server.drift_model_version = self.server.detector.model_version
        _write_json(self, 200, {"status": "initialized", "baselineMeanScore": monitor.baseline_mean_score})

    def _handle_drift_observe(self) -> None:
        try:
            body = _read_json_body(self)
            score = float(body["score"])
        except (KeyError, ValueError, TypeError, OverflowError, json.JSONDecodeError) as e:
            _write_json(self, 400, {"error": f"invalid request: {e}"})
            return
        with self.server.lock:
            monitor = self.server.drift_monitor
            if monitor is None:
                _write_json(self, 409, {"error": "drift monitor not initialized - call POST /drift/init first"})
                return
            if self.server.drift_model_version != self.server.detector.model_version:
                _write_json(
                    self,
                    409,
                    {"error": "the baseline was refitted since the drift monitor was initialized - call POST /drift/init again"},
                )
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
            _write_json(self, 200, {"fitted": fitted, "window": self.server.detector.window, "writeProtected": bool(self.server.token)})
        else:
            _write_json(self, 404, {"error": "not found"})


class DetectorServer(ThreadingHTTPServer):
    """Carries one AnomalyDetector, guarded by a real Lock: fit()/score()
    mutate/read numpy state that (like HYDRA-UMC-DATALAKE's sqlite3
    connection) is not safe to touch from multiple threads at once under
    ThreadingHTTPServer without one."""

    def __init__(
        self,
        address: tuple[str, int],
        detector: AnomalyDetector,
        baseline_path: Path | None = None,
        token: str | None = None,
        protect_reads: bool = False,
    ) -> None:
        super().__init__(address, Handler)
        # With protect_reads (and a token), POST /detect demands the token too; only GET /stats (the health path) stays open.
        self.protect_reads = protect_reads
        # With a token, POST /baseline/fit, /drift/init and /drift/observe demand it (/detect and GET /stats stay
        # open): whoever can reach the port can no longer replace the baseline every reading is judged against.
        self.token = token or None
        self.detector = detector
        self.drift_monitor: DriftMonitor | None = None
        self.drift_model_version = 0
        self.lock = threading.Lock()
        # Optional. None (the default) means every existing
        # behavior is unchanged: POST /baseline/fit only ever updated
        # in-memory state, exactly as before this option existed.
        self.baseline_path = baseline_path
