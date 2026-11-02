# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - tests/test_machine_orders.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Characteristic frequencies and order tracking, checked with synthetic signals only (never a real machine)."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import numpy as np
import pytest

from hydra_umc_anomaly_detector.api import DetectorServer
from hydra_umc_anomaly_detector.detector import AnomalyDetector
from hydra_umc_anomaly_detector.fft import compute_spectrum
from hydra_umc_anomaly_detector.machine import (
    LANGUAGES,
    MESSAGES,
    NO_MATCH,
    BearingSpec,
    DiagnosisSettings,
    Machine,
    MachineError,
    characteristic_lines,
    diagnose,
    message,
    parse_diagnosis_settings,
)
from hydra_umc_anomaly_detector.orders import (
    OrderError,
    OrderSettings,
    max_clean_order,
    order_spectrum,
    parse_order_settings,
    resample_by_angle,
)

FS = 5000.0
N = 4096
BEARING = BearingSpec("drive end", balls=9, ball_diameter=7.94, pitch_diameter=39.04)
MACHINE = Machine("spindle", bearings=(BEARING,), belt_pulley_diameter=60.0, belt_length=900.0, gear_teeth=(17,))
SETTINGS = DiagnosisSettings(enabled=True, machines={"spindle": MACHINE})


def _signal(rpm: float | np.ndarray, orders: dict[float, float], seed: int = 1, noise: float = 0.05, n: int = N) -> np.ndarray:
    """Components at given orders of the shaft speed (order -> amplitude), speed constant or per sample, plus noise."""
    rng = np.random.default_rng(seed)
    speed = np.full(n, float(rpm)) if np.isscalar(rpm) else np.asarray(rpm, dtype=float)
    phase = np.concatenate(([0.0], np.cumsum((speed[:-1] + speed[1:]) / 2.0 / 60.0 / FS)))  # revolutions
    out = noise * rng.standard_normal(n)
    for order, amplitude in orders.items():
        out = out + amplitude * np.sin(2.0 * np.pi * order * phase + 0.7 * order)
    return out


def test_bearing_orders_follow_the_formulas() -> None:
    o = BEARING.orders()
    assert o["bpfo"] + o["bpfi"] == pytest.approx(9.0)  # n/2(1-r) + n/2(1+r)
    assert o["bpfo"] == pytest.approx(3.5848, abs=1e-3)
    assert o["bpfi"] == pytest.approx(5.4152, abs=1e-3)
    assert o["ftf"] == pytest.approx(o["bpfo"] / 9.0)
    assert o["bsf"] == pytest.approx(2.3, abs=0.1)
    angled = BearingSpec("a", 9, 7.94, 39.04, 30.0).orders()
    assert angled["bpfo"] > o["bpfo"] and angled["bpfi"] < o["bpfi"]  # a contact angle lowers the factor


def test_lines_include_belt_gear_and_shaft() -> None:
    codes = [(ln.code, ln.order) for ln in characteristic_lines(MACHINE)]
    assert ("shaft", 1.0) in codes and ("gear_mesh", 17.0) in codes
    belt = next(o for c, o in codes if c == "belt")
    assert belt == pytest.approx(np.pi * 60.0 / 900.0)


def test_outer_race_defect_is_named_not_a_bin() -> None:
    rpm = 1800.0
    o = BEARING.orders()
    sig = _signal(rpm, {1.0: 1.0, o["bpfo"]: 0.6, 2 * o["bpfo"]: 0.4})
    result = diagnose(compute_spectrum(sig, FS, "hann"), rpm, MACHINE, SETTINGS)
    codes = [f["code"] for f in result["findings"]]  # type: ignore[union-attr]
    assert "bpfo" in codes and "bpfi" not in codes and "bsf" not in codes
    bpfo = next(f for f in result["findings"] if f["code"] == "bpfo")  # type: ignore[union-attr]
    assert bpfo["confidence"] == "high" and len(bpfo["hits"]) >= 2
    assert bpfo["expectedHz"] == pytest.approx(o["bpfo"] * 30.0)
    assert "outer-race" in result["summary"]  # type: ignore[operator]
    assert "pista exterior" in diagnose(compute_spectrum(sig, FS, "hann"), rpm, MACHINE, SETTINGS, "es")["summary"]  # type: ignore[operator]


def test_inner_race_and_gear_are_told_apart() -> None:
    rpm = 1500.0
    o = BEARING.orders()
    inner = diagnose(compute_spectrum(_signal(rpm, {o["bpfi"]: 0.7, 2 * o["bpfi"]: 0.5}), FS, "hann"), rpm, MACHINE, SETTINGS)
    assert inner["findings"][0]["code"] == "bpfi"  # type: ignore[index]
    gear = diagnose(compute_spectrum(_signal(rpm, {17.0: 0.8}), FS, "hann"), rpm, MACHINE, SETTINGS)
    assert [f["code"] for f in gear["findings"]] == ["gear_mesh"]  # type: ignore[union-attr]
    assert gear["findings"][0]["confidence"] == "low"  # type: ignore[index]  # one harmonic only


def test_a_healthy_noise_window_finds_nothing() -> None:
    result = diagnose(compute_spectrum(_signal(1800.0, {}, noise=0.3), FS, "hann"), 1800.0, MACHINE, SETTINGS)
    assert result["findings"] == [] and result["summary"] == NO_MATCH["en"]


def test_an_unrelated_peak_is_not_a_finding() -> None:
    # a peak at 4.4 orders is between the characteristic lines (and their harmonics)
    result = diagnose(compute_spectrum(_signal(1800.0, {4.4: 0.9}), FS, "hann"), 1800.0, MACHINE, SETTINGS)
    assert result["findings"] == []


def test_every_message_exists_in_the_eight_languages() -> None:
    assert LANGUAGES == ("en", "es", "de", "fr", "it", "ja", "zh", "pt")
    for code, texts in MESSAGES.items():
        assert set(texts) == set(LANGUAGES), code
        assert len({texts[lang] for lang in LANGUAGES}) == 8, code
    assert set(NO_MATCH) == set(LANGUAGES)
    assert "veio" in message("shaft", "pt") and "rolamento Z" in message("bpfo", "pt", "Z")
    with pytest.raises(MachineError):
        message("bpfo", "xx")


def test_diagnose_refuses_bad_input() -> None:
    spectrum = compute_spectrum(_signal(1800.0, {1.0: 1.0}), FS, "hann")
    for rpm in (5.0, float("nan"), True, "x"):
        with pytest.raises(MachineError):
            diagnose(spectrum, rpm, MACHINE, SETTINGS)  # type: ignore[arg-type]
    with pytest.raises(MachineError):
        diagnose(spectrum, 1800.0, MACHINE, SETTINGS, "xx")


def test_settings_are_off_by_default_and_validated() -> None:
    assert parse_diagnosis_settings(None).enabled is False
    assert parse_order_settings(None).enabled is False
    good = {"enabled": True, "machines": {"m": {"bearings": [{"name": "b", "balls": 8, "ballDiameter": 5, "pitchDiameter": 30}]}}}
    assert parse_diagnosis_settings(good).machines["m"].bearings[0].balls == 8
    bad: list[object] = [
        {"enabled": True},  # enabled with no machine
        {"machines": {"m": {}}},
        {"machines": {"m": {"bearings": [{"name": "b", "balls": 8, "ballDiameter": 40, "pitchDiameter": 30}]}}},
        {"machines": {"m": {"bearings": [{"name": "b", "balls": 2, "ballDiameter": 5, "pitchDiameter": 30}]}}},
        {"harmonics": 0}, {"peakRatio": 1.0}, {"surprise": 1}, {"enabled": "yes"}, "x",
        {"machines": {"m": {"belt": {"pulleyDiameter": 5}}}},
    ]
    for raw in bad:
        with pytest.raises(MachineError):
            parse_diagnosis_settings(raw)
    for raw_order in ({"samplesPerRev": 4}, {"revolutions": 1}, {"enabled": 1}, {"minRpm": 0}, {"x": 1}, []):
        with pytest.raises(OrderError):
            parse_order_settings(raw_order)


# ---- order tracking -------------------------------------------------------------------------------------------

def test_constant_speed_puts_the_peak_on_its_order() -> None:
    rpm = 1800.0
    sig = _signal(rpm, {1.0: 1.0, 3.585: 0.5}, noise=0.01)
    spec = order_spectrum(sig, FS, rpm, OrderSettings(samples_per_rev=64, revolutions=20))
    assert spec.freqs[int(np.argmax(spec.magnitudes))] == pytest.approx(1.0, abs=0.02)
    near = (spec.freqs > 3.4) & (spec.freqs < 3.8)
    assert spec.freqs[near][int(np.argmax(spec.magnitudes[near]))] == pytest.approx(3.585, abs=0.03)


def test_a_speed_ramp_keeps_the_peak_sharp_in_orders_but_not_in_hertz() -> None:
    speed = np.linspace(1500.0, 2100.0, N)
    sig = _signal(speed, {1.0: 1.0, 3.585: 0.6}, noise=0.01)
    cfg = OrderSettings(samples_per_rev=64, revolutions=0)
    orders = order_spectrum(sig, FS, speed, cfg)
    hertz = compute_spectrum(sig, FS, "hann")
    assert orders.freqs[int(np.argmax(orders.magnitudes))] == pytest.approx(1.0, abs=0.1)
    # the defect component (0.6) is one tall peak in orders and a smear, a third as high, in hertz (between 25 and 35 Hz of shaft speed)
    band_orders = (orders.freqs > 3.3) & (orders.freqs < 3.9)
    band_hertz = (hertz.freqs > 3.3 * 25.0) & (hertz.freqs < 3.9 * 35.0)
    assert orders.magnitudes[band_orders].max() > 0.5
    assert hertz.magnitudes[band_hertz].max() < 0.3
    found = diagnose(orders, float(speed.mean()), MACHINE, SETTINGS, order_axis=True)
    assert "bpfo" in [f["code"] for f in found["findings"]]  # type: ignore[union-attr]


def test_speed_changes_do_not_raise_false_alarms_in_orders() -> None:
    cfg = OrderSettings(samples_per_rev=64, revolutions=20)

    def healthy(seed: int, lo: float, hi: float) -> tuple[np.ndarray, np.ndarray]:
        speed = np.linspace(lo, hi, N)
        return _signal(speed, {1.0: 1.0, 2.0: 0.3}, seed=seed), speed

    rng = np.random.default_rng(7)
    train = []
    for i in range(24):
        lo = float(rng.uniform(1700, 1900))
        sig, speed = healthy(i, lo, lo + float(rng.uniform(-80, 80)))
        train.append(resample_by_angle(sig, FS, speed, cfg.samples_per_rev, cfg.revolutions))
    by_angle = AnomalyDetector(sample_rate=float(cfg.samples_per_rev), threshold=10.0, window="hann")
    by_angle.fit(train)
    # a window at a speed and a ramp the baseline never saw: still healthy
    sig, speed = healthy(99, 1950.0, 2050.0)
    assert not by_angle.score(resample_by_angle(sig, FS, speed, cfg.samples_per_rev, cfg.revolutions)).anomalous
    # and a real fault is still found, at the order of the defect
    sick = _signal(speed, {1.0: 1.0, 2.0: 0.3, 3.585: 0.6}, seed=5)
    verdict = by_angle.score(resample_by_angle(sick, FS, speed, cfg.samples_per_rev, cfg.revolutions))
    assert verdict.anomalous and verdict.worst_bin_freq == pytest.approx(3.585, abs=0.05)

    # the same healthy ramps seen in hertz give a spectrum that moves with the speed: the same detector idea fails
    time_windows = [healthy(i, 1700.0 + 10 * i, 1700.0 + 10 * i)[0][:2048] for i in range(24)]
    by_time = AnomalyDetector(sample_rate=FS, threshold=10.0, window="hann")
    by_time.fit(time_windows)
    moved, _ = healthy(98, 2050.0, 2050.0)
    assert by_time.score(moved[:2048]).anomalous  # a different (constant) speed is called an anomaly in hertz


def test_order_tracking_refuses_bad_input() -> None:
    sig = _signal(1800.0, {1.0: 1.0})
    with pytest.raises(OrderError):
        resample_by_angle(sig, FS, 5.0)  # below min_rpm
    with pytest.raises(OrderError):
        resample_by_angle(sig, FS, [1800.0] * 10)  # wrong length
    with pytest.raises(OrderError):
        resample_by_angle(sig, FS, 1800.0, revolutions=1000)  # the window is shorter than that
    with pytest.raises(OrderError):
        resample_by_angle(sig[:50], FS, 1800.0)  # less than two revolutions
    bad = sig.copy()
    bad[3] = float("nan")
    with pytest.raises(OrderError):
        resample_by_angle(bad, FS, 1800.0)
    with pytest.raises(OrderError):
        resample_by_angle(sig, FS, [float("inf")] * N)
    assert max_clean_order(FS, 3000.0) == pytest.approx(50.0)


# ---- the route ------------------------------------------------------------------------------------------------

def _post(url: str, body: dict, token: str | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _run(**kwargs: object) -> tuple[DetectorServer, threading.Thread]:
    server = DetectorServer(("127.0.0.1", 0), AnomalyDetector(sample_rate=FS), **kwargs)  # type: ignore[arg-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _stop(server: DetectorServer, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_route_is_404_while_off() -> None:
    server, thread = _run()
    try:
        status, _ = _post(f"http://127.0.0.1:{server.server_address[1]}/diagnose", {"window": [1.0] * 64, "rpm": 1800, "machine": "spindle"})
        assert status == 404
    finally:
        _stop(server, thread)


def test_route_names_the_defect_and_validates() -> None:
    server, thread = _run(diagnosis=SETTINGS, order_tracking=OrderSettings(enabled=True))
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/diagnose"
        o = BEARING.orders()
        window = _signal(1800.0, {1.0: 1.0, o["bpfo"]: 0.6, 2 * o["bpfo"]: 0.4}).tolist()
        status, body = _post(url, {"window": window, "rpm": 1800, "machine": "spindle", "lang": "de"})
        assert status == 200 and "Außenring" in body["summary"] and body["axis"] == "hz"
        speed = np.linspace(1500.0, 2100.0, N)
        ramp = _signal(speed, {1.0: 1.0, o["bpfo"]: 0.6, 2 * o["bpfo"]: 0.4}).tolist()
        status, body = _post(url, {"window": ramp, "rpm": speed.tolist(), "machine": "spindle"})
        assert status == 200 and body["axis"] == "orders" and any(f["code"] == "bpfo" for f in body["findings"])
        for bad in (
            {"window": window, "rpm": 1800, "machine": "nope"},
            {"window": window, "rpm": 1800},
            {"window": window, "machine": "spindle"},
            {"window": window, "rpm": 5, "machine": "spindle"},
            {"window": window, "rpm": 1800, "machine": "spindle", "lang": "xx"},
            {"window": window, "rpm": [1800.0] * 3, "machine": "spindle"},
            {"window": [], "rpm": 1800, "machine": "spindle"},
        ):
            assert _post(url, bad)[0] == 400
    finally:
        _stop(server, thread)


def test_a_speed_per_sample_needs_order_tracking_on() -> None:
    server, thread = _run(diagnosis=SETTINGS)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/diagnose"
        status, body = _post(url, {"window": [0.5] * 256, "rpm": [1800.0] * 256, "machine": "spindle"})
        assert status == 400 and "orderTracking" in body["error"]
    finally:
        _stop(server, thread)


def test_route_needs_the_token_when_reads_are_protected() -> None:
    server, thread = _run(diagnosis=SETTINGS, token="a-long-enough-token-1234")
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/diagnose"
        req = {"window": _signal(1800.0, {1.0: 1.0}).tolist(), "rpm": 1800, "machine": "spindle"}
        assert _post(url, req)[0] == 401
        assert _post(url, req, "a-long-enough-token-1234")[0] == 200
    finally:
        _stop(server, thread)
