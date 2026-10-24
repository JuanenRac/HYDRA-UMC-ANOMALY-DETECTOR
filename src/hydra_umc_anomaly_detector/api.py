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
from urllib.parse import unquote, urlparse

from .baseline import BaselineError, save_baseline
from .detector import AnomalyDetector, NotFittedError
from .drift import DriftMonitor, DriftMonitorError
from .registry import DetectorRegistry, RegistryError, RegistryFull, parse_key


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
# what can be asked of ONE series, after /detectors/{sourceId}/{kind}/{field}/
_SERIES_OPERATIONS = ("fit", "detect", "drift/init", "drift/observe")


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
        elif path.startswith("/detectors/"):
            self._handle_series(path)
        else:
            _write_json(self, 404, {"error": "not found"})

    def _handle_series(self, path: str) -> None:
        """POST /detectors/{sourceId}/{kind}/{field}/{fit|detect|drift/init|drift/observe}: the same operations on the detector of ONE series."""
        parts = path.split("/")[2:]
        if len(parts) < 4 or "/".join(parts[3:]) not in _SERIES_OPERATIONS:
            _write_json(self, 404, {"error": "not found"})
            return
        operation = "/".join(parts[3:])
        try:
            key = parse_key(unquote(parts[0]), unquote(parts[1]), unquote(parts[2]))
        except RegistryError as error:
            _write_json(self, 400, {"error": str(error)})
            return
        # writing a series needs the token like writing the shared baseline; scoring needs it only with --protect-reads
        if (operation != "detect" or self.server.protect_reads) and not _authorized(self):
            _write_json(self, 401, {"error": "a valid bearer token is required for this operation"})
            return
        registry = self.server.registry
        if operation == "fit":
            existing = registry.get(key)
            if existing is not None:
                self._handle_fit(existing)
                return
            # a series that does not exist yet: fit a fresh detector, and give it its place only if the fit works
            fresh = registry.new_detector()
            holder = type("Fresh", (), {"detector": fresh, "lock": threading.Lock(), "baseline_path": None, "drift_monitor": None, "drift_model_version": 0})()
            self._handle_fit(holder, key)
            return
        entry = registry.get(key)
        if entry is None:
            _write_json(self, 409, {"error": f"the series {key.source_id}/{key.kind}/{key.field} has no baseline yet - fit it first with POST /detectors/{{sourceId}}/{{kind}}/{{field}}/fit"})
            return
        if operation == "detect":
            self._handle_detect(entry)
        elif operation == "drift/init":
            self._handle_drift_init(entry)
        else:
            self._handle_drift_observe(entry)

    def _handle_fit(self, ctx: object = None, registry_key: object = None) -> None:
        ctx = self.server if ctx is None else ctx
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
            prepared = ctx.detector.prepare_fit(windows)
            if registry_key is not None:
                ctx = self.server.registry.insert(registry_key, ctx.detector)
            with ctx.lock:
                ctx.detector.adopt(prepared)
                if ctx.baseline_path is not None:
                    # The fitted baseline used to live only in memory -
                    # persist it now, inside the
                    # same lock, so a concurrent GET /stats or POST
                    # /detect never observes a fit that hasn't actually
                    # been saved yet.
                    save_baseline(ctx.baseline_path, ctx.detector.baseline, ctx.detector.model_version)
        except RegistryFull as e:
            _write_json(self, 409, {"error": str(e)})
            return
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

    def _handle_detect(self, ctx: object = None) -> None:
        ctx = self.server if ctx is None else ctx
        try:
            body = _read_json_body(self)
            window = body["window"]
            if not isinstance(window, list) or not window:
                raise ValueError("\"window\" must be a non-empty array")
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as e:
            _write_json(self, 400, {"error": f"invalid request: {e}"})
            return
        try:
            with ctx.lock:
                verdict = ctx.detector.score(window)
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

    def _handle_drift_init(self, ctx: object = None) -> None:
        ctx = self.server if ctx is None else ctx
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
        with ctx.lock:
            ctx.drift_monitor = monitor
            # the reference scores belong to the model that exists right now
            ctx.drift_model_version = ctx.detector.model_version
        _write_json(self, 200, {"status": "initialized", "baselineMeanScore": monitor.baseline_mean_score})

    def _handle_drift_observe(self, ctx: object = None) -> None:
        ctx = self.server if ctx is None else ctx
        try:
            body = _read_json_body(self)
            score = float(body["score"])
        except (KeyError, ValueError, TypeError, OverflowError, json.JSONDecodeError) as e:
            _write_json(self, 400, {"error": f"invalid request: {e}"})
            return
        with ctx.lock:
            monitor = ctx.drift_monitor
            if monitor is None:
                _write_json(self, 409, {"error": "drift monitor not initialized - call POST /drift/init first"})
                return
            if ctx.drift_model_version != ctx.detector.model_version:
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
        path = urlparse(self.path).path
        if path == "/stats":
            with self.server.lock:
                fitted = self.server.detector.is_fitted
            stats = {"fitted": fitted, "window": self.server.detector.window, "writeProtected": bool(self.server.token)}
            if self.server.follower_snapshot is not None:
                stats["follower"] = self.server.follower_snapshot()
            _write_json(self, 200, stats)
        elif path == "/detectors":
            # which series have a baseline: what a client needs before it scores one (the same openness as /detect)
            if self.server.protect_reads and not _authorized(self):
                _write_json(self, 401, {"error": "a valid bearer token is required for this operation"})
                return
            _write_json(self, 200, {"detectors": self.server.registry.listing()})
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
        protect_reads: bool | None = None,
        registry: DetectorRegistry | None = None,
    ) -> None:
        super().__init__(address, Handler)
        # One detector per series (sourceId, kind, field), beside the shared one: see registry.py. Without one, the bound is the default and nothing is saved.
        # Set by the command when the continuous service mode runs: what GET /stats shows of it.
        self.follower_snapshot = None
        self.registry = registry if registry is not None else DetectorRegistry(lambda: AnomalyDetector(sample_rate=detector.sample_rate, threshold=detector.threshold, window=detector.window))
        # With protect_reads (and a token), POST /detect demands the token too; only GET /stats (the health path) stays open.
        # None (the default) means protected whenever a token is set: there is nothing to demand without one.
        self.protect_reads = bool(token) if protect_reads is None else protect_reads
        # With a token, POST /baseline/fit, /drift/init and /drift/observe demand it (GET /stats stays
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
