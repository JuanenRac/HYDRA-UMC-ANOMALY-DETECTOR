# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - tests/test_rul.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from hydra_umc_anomaly_detector.api import DetectorServer
from hydra_umc_anomaly_detector.detector import AnomalyDetector
from hydra_umc_anomaly_detector.rul import RulError, estimate_rul

DAY = 86400.0


def _line(days: int, per_day: float, start: float = 1.0) -> tuple[list[float], list[float]]:
    return [d * DAY for d in range(days)], [start + per_day * d for d in range(days)]


def test_exact_line_gives_exact_days() -> None:
    t, y = _line(20, 0.1)  # 1.0 .. 2.9 at day 19
    est = estimate_rul(t, y, limit=5.0)
    assert est.status == "estimated" and est.points == 20
    assert est.slope_per_day == pytest.approx(0.1)
    assert est.days_to_limit == pytest.approx((5.0 - 2.9) / 0.1)
    assert est.days_low == pytest.approx(est.days_to_limit) and est.days_high == pytest.approx(est.days_to_limit)


def test_robust_to_a_few_wild_points() -> None:
    t, y = _line(30, 0.1)
    y[5] += 50.0
    y[17] -= 40.0
    y[28] += 30.0
    est = estimate_rul(t, y, limit=6.0)
    assert est.slope_per_day == pytest.approx(0.1, abs=0.01)
    assert est.days_to_limit is not None and abs(est.days_to_limit - (6.0 - 3.9) / 0.1) < 3.0


def test_input_order_does_not_matter() -> None:
    t, y = _line(15, 0.2)
    forward = estimate_rul(t, y, 10.0)
    backward = estimate_rul(t[::-1], y[::-1], 10.0)
    assert forward == backward


def test_range_brackets_the_estimate_on_noisy_data() -> None:
    t, y = _line(40, 0.1)
    y = [v + (0.3 if i % 2 else -0.3) for i, v in enumerate(y)]
    est = estimate_rul(t, y, limit=8.0)
    assert est.days_low is not None and est.days_high is not None and est.days_to_limit is not None
    assert est.days_low <= est.days_to_limit <= est.days_high


def test_flat_or_falling_is_not_rising() -> None:
    t, y = _line(12, 0.0)
    flat = estimate_rul(t, y, limit=3.0)
    assert flat.status == "not_rising" and flat.days_to_limit is None
    t, y = _line(12, -0.05, start=2.0)
    assert estimate_rul(t, y, limit=3.0).status == "not_rising"


def test_limit_already_reached() -> None:
    t, y = _line(12, 0.1)
    est = estimate_rul(t, y, limit=1.5)
    assert est.status == "limit_reached" and est.days_to_limit == 0.0


def test_too_few_points_or_one_instant_is_not_an_estimate() -> None:
    assert estimate_rul([0.0, DAY, 2 * DAY], [1.0, 2.0, 3.0], 5.0).status == "insufficient_data"
    assert estimate_rul([5.0] * 10, list(range(10)), 100.0).status == "insufficient_data"
    t, y = _line(5, 0.1)
    assert estimate_rul(t, y, 5.0, min_points=5).status == "estimated"


@pytest.mark.parametrize(
    "times,scores,limit",
    [([0.0, 1.0], [1.0], 2.0), ([0.0] * 10, [float("nan")] * 10, 2.0), ([True] * 10, [1.0] * 10, 2.0), (list(range(10)), list(range(10)), float("inf")), (list(range(10)), list(range(10)), True)],
)
def test_refuses_malformed_input(times: list, scores: list, limit: object) -> None:
    with pytest.raises(RulError):
        estimate_rul(times, scores, limit)  # type: ignore[arg-type]


def test_refuses_too_many_points_and_bad_minimum() -> None:
    with pytest.raises(RulError):
        estimate_rul([float(i) for i in range(1001)], [1.0] * 1001, 2.0)
    with pytest.raises(RulError):
        estimate_rul([0.0, 1.0, 2.0], [1.0, 2.0, 3.0], 5.0, min_points=2)


def _post(url: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _run(**kwargs: object) -> tuple[DetectorServer, threading.Thread]:
    server = DetectorServer(("127.0.0.1", 0), AnomalyDetector(sample_rate=1000.0), **kwargs)  # type: ignore[arg-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _stop(server: DetectorServer, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_route_answers_404_while_off() -> None:
    server, thread = _run()
    try:
        t, y = _line(12, 0.1)
        points = [{"t": a, "score": b} for a, b in zip(t, y)]
        status, _ = _post(f"http://127.0.0.1:{server.server_address[1]}/rul", {"points": points, "limit": 5.0})
        assert status == 404
    finally:
        _stop(server, thread)


def test_route_estimates_when_on() -> None:
    server, thread = _run(rul_enabled=True, rul_min_points=6)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/rul"
        t, y = _line(6, 0.1)
        points = [{"t": a, "score": b} for a, b in zip(t, y)]
        status, body = _post(url, {"points": points, "limit": 3.0})
        assert status == 200 and body["status"] == "estimated" and body["daysToLimit"] == pytest.approx(15.0)
        status, body = _post(url, {"points": points[:4], "limit": 3.0})
        assert status == 200 and body["status"] == "insufficient_data"
        for bad in ({"limit": 3.0}, {"points": points}, {"points": [{"t": 1}], "limit": 3.0}, {"points": "x", "limit": 3.0}):
            status, body = _post(url, bad)
            assert status == 400 and "error" in body
    finally:
        _stop(server, thread)


def test_route_needs_the_token_when_reads_are_protected() -> None:
    server, thread = _run(rul_enabled=True, token="0123456789abcdef-token")
    try:
        status, _ = _post(f"http://127.0.0.1:{server.server_address[1]}/rul", {"points": [], "limit": 1.0})
        assert status == 401
    finally:
        _stop(server, thread)
