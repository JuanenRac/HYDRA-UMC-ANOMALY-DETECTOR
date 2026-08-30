# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - tests/test_api.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""Real HTTP round-trips against a genuine DetectorServer on an ephemeral
loopback port - same standard as HYDRA-UMC-DATALAKE's own tests/test_api.py."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator

import numpy as np
import pytest

from hydra_umc_anomaly_detector.api import MAX_BODY_BYTES, DetectorServer
from hydra_umc_anomaly_detector.detector import AnomalyDetector

SAMPLE_RATE = 1000.0
N_SAMPLES = 200


def _sine(freq_hz: float) -> list[float]:
    t = np.arange(N_SAMPLES) / SAMPLE_RATE
    return list(np.sin(2 * np.pi * freq_hz * t))


@pytest.fixture()
def server_url() -> Iterator[str]:
    detector = AnomalyDetector(sample_rate=SAMPLE_RATE, threshold=4.0)
    server = DetectorServer(("127.0.0.1", 0), detector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _post(url: str, payload: dict) -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(url: str) -> tuple[int, object]:
    with urllib.request.urlopen(url) as resp:
        return resp.status, json.loads(resp.read())


def test_detect_before_fit_is_409(server_url: str) -> None:
    status, body = _post(f"{server_url}/detect", {"window": _sine(50.0)})
    assert status == 409
    assert "error" in body


def test_stats_reports_unfitted_then_fitted(server_url: str) -> None:
    status, body = _get(f"{server_url}/stats")
    assert status == 200
    assert body == {"fitted": False}

    windows = [_sine(50.0) for _ in range(5)]
    status, _ = _post(f"{server_url}/baseline/fit", {"windows": windows})
    assert status == 200

    status, body = _get(f"{server_url}/stats")
    assert body == {"fitted": True}


def test_fit_then_detect_real_round_trip(server_url: str) -> None:
    windows = [_sine(50.0) for _ in range(5)]
    status, body = _post(f"{server_url}/baseline/fit", {"windows": windows})
    assert status == 200
    assert body == {"status": "fitted", "windowCount": 5}

    status, body = _post(f"{server_url}/detect", {"window": _sine(50.0)})
    assert status == 200
    assert body["anomalous"] is False

    faulty = list(np.array(_sine(50.0)) + 0.8 * np.sin(2 * np.pi * 137.0 * np.arange(N_SAMPLES) / SAMPLE_RATE))
    status, body = _post(f"{server_url}/detect", {"window": faulty})
    assert status == 200
    assert body["anomalous"] is True


def test_fit_rejects_empty_windows_list(server_url: str) -> None:
    status, body = _post(f"{server_url}/baseline/fit", {"windows": []})
    assert status == 400
    assert "error" in body


def test_fit_rejects_mismatched_window_lengths(server_url: str) -> None:
    status, body = _post(f"{server_url}/baseline/fit", {"windows": [_sine(50.0), _sine(50.0)[:100]]})
    assert status == 400
    assert "error" in body


def test_fit_rejects_too_short_window_as_400_not_500(server_url: str) -> None:
    # Real regression case: compute_spectrum() (called via fit_baseline())
    # raises a plain ValueError for a window with fewer than 2 samples -
    # that must come back as a clean 400, not crash the request thread.
    status, body = _post(f"{server_url}/baseline/fit", {"windows": [[5], [5]]})
    assert status == 400
    assert "error" in body


def test_fit_rejects_non_numeric_window_as_400_not_500(server_url: str) -> None:
    # Real regression case: compute_spectrum()'s own np.asarray(..., dtype=
    # float64) raises a plain ValueError for non-numeric values - also a
    # clean 400, not a crash.
    status, body = _post(f"{server_url}/baseline/fit", {"windows": [["a", "b"], ["c", "d"]]})
    assert status == 400
    assert "error" in body


def test_detect_rejects_too_short_window_as_400_not_500(server_url: str) -> None:
    # Real regression case: compute_spectrum() (called via score()) raises
    # a plain ValueError for a window with fewer than 2 samples once the
    # detector is fitted - that must come back as a clean 400, not crash
    # the request thread.
    windows = [_sine(50.0) for _ in range(5)]
    _post(f"{server_url}/baseline/fit", {"windows": windows})

    status, body = _post(f"{server_url}/detect", {"window": [5]})
    assert status == 400
    assert "error" in body


def test_detect_rejects_non_numeric_window_as_400_not_500(server_url: str) -> None:
    # Real regression case: compute_spectrum()'s own np.asarray(..., dtype=
    # float64) raises a plain ValueError for non-numeric values - also a
    # clean 400, not a crash.
    windows = [_sine(50.0) for _ in range(5)]
    _post(f"{server_url}/baseline/fit", {"windows": windows})

    status, body = _post(f"{server_url}/detect", {"window": ["a", "b"]})
    assert status == 400
    assert "error" in body


def test_detect_rejects_a_non_finite_window_sample_as_400_not_500(server_url: str) -> None:
    # Real end-to-end regression: json.dumps/json.loads both accept the
    # non-standard NaN token by default on this stdlib round-trip, so a
    # real client CAN put one on the wire without ever hitting a JSON
    # encode/decode error - only compute_spectrum()'s own explicit finite
    # check (exercised here through the real HTTP surface, not just the
    # unit test in test_fft.py) stands between that and a silently
    # corrupted verdict.
    windows = [_sine(50.0) for _ in range(5)]
    _post(f"{server_url}/baseline/fit", {"windows": windows})

    status, body = _post(f"{server_url}/detect", {"window": [1.0, float("nan"), 3.0]})
    assert status == 400
    assert "error" in body


def test_detect_response_carries_real_model_version_and_threshold(server_url: str) -> None:
    windows = [_sine(50.0) for _ in range(5)]
    _post(f"{server_url}/baseline/fit", {"windows": windows})

    status, body = _post(f"{server_url}/detect", {"window": _sine(50.0)})

    assert status == 200
    assert body["modelVersion"] == 1
    assert body["threshold"] == 4.0


def test_drift_observe_before_init_is_409(server_url: str) -> None:
    status, body = _post(f"{server_url}/drift/observe", {"score": 5.0})
    assert status == 409
    assert "error" in body


def test_drift_init_rejects_empty_baseline(server_url: str) -> None:
    status, body = _post(f"{server_url}/drift/init", {"baselineScores": []})
    assert status == 400
    assert "error" in body


def test_drift_init_rejects_a_non_finite_baseline_score(server_url: str) -> None:
    status, body = _post(f"{server_url}/drift/init", {"baselineScores": [1.0, float("nan")]})
    assert status == 400
    assert "error" in body


def test_drift_observe_rejects_a_non_finite_score(server_url: str) -> None:
    _post(f"{server_url}/drift/init", {"baselineScores": [5.0, 5.0, 5.0], "windowSize": 3})
    status, body = _post(f"{server_url}/drift/observe", {"score": float("inf")})
    assert status == 400
    assert "error" in body


def test_oversized_json_request_is_rejected_before_parsing(server_url: str) -> None:
    raw = b"{" + b"x" * MAX_BODY_BYTES
    request = urllib.request.Request(
        f"{server_url}/detect",
        data=raw,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code == 400
    assert "1048576" in json.loads(error.value.read())["error"]


def test_drift_real_end_to_end_round_trip(server_url: str) -> None:
    status, body = _post(
        f"{server_url}/drift/init",
        {"baselineScores": [5.0, 5.0, 5.0], "windowSize": 3, "driftRatioThreshold": 2.0},
    )
    assert status == 200
    assert body["status"] == "initialized"
    assert body["baselineMeanScore"] == 5.0

    status, body = _post(f"{server_url}/drift/observe", {"score": 5.0})
    assert status == 200
    assert body["status"] == "priming"

    _post(f"{server_url}/drift/observe", {"score": 5.0})
    status, body = _post(f"{server_url}/drift/observe", {"score": 5.0})
    assert status == 200
    assert body["status"] == "ready"
    assert body["drifted"] is False

    for _ in range(3):
        status, body = _post(f"{server_url}/drift/observe", {"score": 50.0})
    assert body["drifted"] is True
    assert body["driftRatio"] == pytest.approx(10.0)
