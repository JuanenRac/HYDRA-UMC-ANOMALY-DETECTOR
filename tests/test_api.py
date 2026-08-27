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

from hydra_umc_anomaly_detector.api import DetectorServer
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
