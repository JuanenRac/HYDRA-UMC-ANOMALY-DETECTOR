# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - tests/test_datalake_client.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""Found missing in an ecosystem-wide software-improvements audit: no
code anywhere in this repo ever queried HYDRA-UMC-DATALAKE, despite the
README describing exactly that. Real end-to-end tests against a real
local HTTP server (fake_datalake.py) implementing DATALAKE's actual
GET /query contract - not a mocked function call.
"""
from __future__ import annotations

import pytest

from fake_datalake import running_fake_datalake
from hydra_umc_anomaly_detector.datalake_client import DatalakeClient, DatalakeError, fetch_windows


def test_query_real_round_trip() -> None:
    with running_fake_datalake() as (url, server):
        server.points = [
            {"sourceId": "motor-1", "kind": "vibration", "field": "value", "timestamp": 1000, "value": 1.5},
            {"sourceId": "motor-1", "kind": "vibration", "field": "value", "timestamp": 2000, "value": 2.5},
        ]
        client = DatalakeClient(url)
        points = client.query(source_id="motor-1", kind="vibration", field="value")
        assert [p.value for p in points] == [1.5, 2.5]


def test_unreachable_datalake_raises_datalake_error() -> None:
    client = DatalakeClient("http://127.0.0.1:1", timeout_s=1.0, max_attempts=1)
    with pytest.raises(DatalakeError):
        client.query(source_id="motor-1")


def test_http_error_from_datalake_raises_datalake_error() -> None:
    with running_fake_datalake() as (url, _server):
        client = DatalakeClient(url)
        with pytest.raises(DatalakeError):
            client._get("/error", {})


# ---------------------------------------------------------------------------
# fetch_windows() - the real gap: turning raw DATALAKE points into the
# fixed-length windows fit_baseline()/AnomalyDetector.score() require.
# ---------------------------------------------------------------------------


def _points(values: list[float]) -> list[dict]:
    return [
        {"sourceId": "motor-1", "kind": "vibration", "field": "value", "timestamp": 1000 + i, "value": v}
        for i, v in enumerate(values)
    ]


def test_fetch_windows_chunks_consecutive_non_overlapping_windows() -> None:
    with running_fake_datalake() as (url, server):
        server.points = _points([float(i) for i in range(10)])
        client = DatalakeClient(url)

        windows = fetch_windows(client, source_id="motor-1", kind="vibration", field="value", window_size=4)

        assert windows == [[0.0, 1.0, 2.0, 3.0], [4.0, 5.0, 6.0, 7.0]]  # 2 left over (8, 9) dropped


def test_fetch_windows_drops_a_trailing_partial_window() -> None:
    with running_fake_datalake() as (url, server):
        server.points = _points([1.0, 2.0, 3.0])  # fewer than one full window
        client = DatalakeClient(url)

        windows = fetch_windows(client, source_id="motor-1", kind="vibration", field="value", window_size=4)

        assert windows == []


def test_fetch_windows_rejects_a_non_positive_window_size() -> None:
    with running_fake_datalake() as (url, _server):
        client = DatalakeClient(url)
        with pytest.raises(ValueError):
            fetch_windows(client, source_id="motor-1", kind="vibration", field="value", window_size=0)
