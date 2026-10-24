# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - tests/test_series.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""One detector per series: the registry on its own, then real HTTP round-trips against a genuine DetectorServer."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest

from hydra_umc_anomaly_detector.api import DetectorServer
from hydra_umc_anomaly_detector.detector import AnomalyDetector
from hydra_umc_anomaly_detector.registry import DetectorRegistry, RegistryError, RegistryFull, SeriesKey, parse_key

SAMPLE_RATE = 1000.0
N_SAMPLES = 200
TOKEN = "a-secret-of-sixteen-characters"


def _sine(freq_hz: float, amplitude: float = 1.0) -> list[float]:
    t = np.arange(N_SAMPLES) / SAMPLE_RATE
    return list(amplitude * np.sin(2 * np.pi * freq_hz * t))


def _noisy(freq_hz: float, seed: int) -> list[float]:
    # a healthy reading of this machine: the same every time (the detector's own tests use identical windows too)
    return _sine(freq_hz)


def _factory() -> AnomalyDetector:
    return AnomalyDetector(sample_rate=SAMPLE_RATE, threshold=4.0)


# --- the registry ----------------------------------------------------------------------------------------------------------


def test_a_key_is_three_stable_identifiers():
    assert parse_key("robot-1", "motor_vibration", "value") == SeriesKey("robot-1", "motor_vibration", "value")
    for bad in ("", " x", "a b", "../x", "a/b", "x" * 97, None, 5):
        with pytest.raises(RegistryError):
            parse_key(bad, "k", "f")  # type: ignore[arg-type]
        with pytest.raises(RegistryError):
            parse_key("s", bad, "f")  # type: ignore[arg-type]
        with pytest.raises(RegistryError):
            parse_key("s", "k", bad)  # type: ignore[arg-type]


def test_the_digest_of_a_key_never_depends_on_its_text_as_a_path():
    assert parse_key("s", "k", "f").digest() != parse_key("s", "k", "g").digest()
    assert "/" not in parse_key("s", "k", "f").digest()
    assert parse_key("a", "bc", "d").digest() != parse_key("ab", "c", "d").digest()  # the separator matters


def test_a_series_exists_only_after_it_was_inserted():
    registry = DetectorRegistry(_factory)
    key = parse_key("s", "k", "f")
    assert registry.get(key) is None
    assert len(registry) == 0
    entry = registry.insert(key, registry.new_detector())
    assert registry.get(key) is entry
    assert registry.insert(key, registry.new_detector()) is entry  # the one already there wins


def test_the_registry_is_bounded():
    registry = DetectorRegistry(_factory, max_detectors=2)
    registry.insert(parse_key("a", "k", "f"), registry.new_detector())
    registry.insert(parse_key("b", "k", "f"), registry.new_detector())
    with pytest.raises(RegistryFull):
        registry.insert(parse_key("c", "k", "f"), registry.new_detector())
    with pytest.raises(ValueError):
        DetectorRegistry(_factory, max_detectors=0)


def test_the_listing_says_which_series_are_fitted():
    registry = DetectorRegistry(_factory)
    fitted = registry.new_detector()
    fitted.fit([_sine(50.0) for _ in range(5)])
    registry.insert(parse_key("b", "k", "f"), fitted)
    registry.insert(parse_key("a", "k", "f"), registry.new_detector())
    rows = registry.listing()
    assert [(r["sourceId"], r["fitted"]) for r in rows] == [("a", False), ("b", True)]


def test_baselines_are_restored_from_the_directory(tmp_path: Path):
    from hydra_umc_anomaly_detector.baseline import save_baseline

    registry = DetectorRegistry(_factory, tmp_path)
    key = parse_key("robot-1", "vibration", "value")
    detector = registry.new_detector()
    detector.fit([_sine(50.0) for _ in range(5)])
    entry = registry.insert(key, detector)
    save_baseline(entry.baseline_path, detector.baseline, detector.model_version)

    again = DetectorRegistry(_factory, tmp_path)
    restored, problems = again.restore_all()
    assert (restored, problems) == (1, [])
    assert again.get(key).detector.is_fitted
    assert again.get(key).detector.model_version == detector.model_version


def test_a_damaged_or_foreign_file_is_reported_not_restored(tmp_path: Path):
    registry = DetectorRegistry(_factory, tmp_path)
    key = parse_key("robot-1", "vibration", "value")
    registry.insert(key, registry.new_detector())  # a key file with no baseline beside it
    (tmp_path / "ffff.key.json").write_text("not json", encoding="utf-8")
    (tmp_path / f"{'0' * 40}.key.json").write_text(json.dumps({"sourceId": "s", "kind": "k", "field": "f"}), encoding="utf-8")
    restored, problems = DetectorRegistry(_factory, tmp_path).restore_all()
    assert restored == 0
    assert len(problems) == 3  # one per file, none of them a crash


def test_a_hostile_source_id_never_becomes_a_path(tmp_path: Path):
    registry = DetectorRegistry(_factory, tmp_path)
    with pytest.raises(RegistryError):
        registry.insert(parse_key("../../etc", "k", "f"), registry.new_detector())
    assert list(tmp_path.iterdir()) == []


# --- over HTTP -------------------------------------------------------------------------------------------------------------


@pytest.fixture()
def server(tmp_path: Path) -> Iterator[DetectorServer]:
    registry = DetectorRegistry(_factory, tmp_path / "series", max_detectors=3)
    srv = DetectorServer(("127.0.0.1", 0), _factory(), token=TOKEN, registry=registry)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


def _url(server: DetectorServer, path: str) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}{path}"


def _post(url: str, payload: dict, token: str | None = TOKEN) -> tuple[int, dict]:
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers={"Content-Type": "application/json"})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def _get(url: str, token: str | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def fit(server, source, windows=None, kind="vibration", field="value"):
    return _post(_url(server, f"/detectors/{source}/{kind}/{field}/fit"), {"windows": windows or [_noisy(50.0, i) for i in range(8)]})


def test_two_series_keep_two_baselines(server):
    assert fit(server, "motor", [_noisy(50.0, i) for i in range(8)])[0] == 200
    assert fit(server, "spindle", [_noisy(120.0, i) for i in range(8)])[0] == 200
    motor = _post(_url(server, "/detectors/motor/vibration/value/detect"), {"window": _noisy(50.0, 99)})[1]
    spindle_on_motor = _post(_url(server, "/detectors/spindle/vibration/value/detect"), {"window": _noisy(50.0, 99)})[1]
    # the motor's own healthy signal is normal for the motor and anomalous for the spindle: one baseline for both could not say that
    assert motor["anomalous"] is False
    assert spindle_on_motor["anomalous"] is True


def test_the_series_does_not_touch_the_shared_detector(server):
    fit(server, "motor")
    assert _get(_url(server, "/stats"))[1]["fitted"] is False
    assert _post(_url(server, "/detect"), {"window": _sine(50.0)}, token=None)[0] == 409


def test_scoring_a_series_that_was_never_fitted_is_409_never_another_baseline(server):
    fit(server, "motor")
    status, body = _post(_url(server, "/detectors/other/vibration/value/detect"), {"window": _sine(50.0)}, token=None)
    assert status == 409
    assert "fit it first" in body["error"]


def test_a_failed_first_fit_takes_no_place(server):
    status, _ = _post(_url(server, "/detectors/motor/vibration/value/fit"), {"windows": [[5], [5]]})
    assert status == 400
    assert _get(_url(server, "/detectors"))[1]["detectors"] == []
    for source in ("a", "b", "c"):  # all three places are still free
        assert fit(server, source)[0] == 200


def test_the_bound_is_a_409(server):
    for source in ("a", "b", "c"):
        assert fit(server, source)[0] == 200
    status, body = fit(server, "d")
    assert status == 409
    assert "no more" in body["error"]
    assert fit(server, "a")[0] == 200  # an existing series can always be refitted


def test_refitting_a_series_raises_its_model_version(server):
    fit(server, "motor")
    first = _post(_url(server, "/detectors/motor/vibration/value/detect"), {"window": _noisy(50.0, 1)}, token=None)[1]["modelVersion"]
    fit(server, "motor")
    second = _post(_url(server, "/detectors/motor/vibration/value/detect"), {"window": _noisy(50.0, 1)}, token=None)[1]["modelVersion"]
    assert second == first + 1


def test_writing_a_series_needs_the_token_and_scoring_does_not_by_default(server):
    url = _url(server, "/detectors/motor/vibration/value/fit")
    assert _post(url, {"windows": [_sine(50.0)] * 5}, token=None)[0] == 401
    assert _post(url, {"windows": [_sine(50.0)] * 5}, token="wrong-token-of-sixteen-chars")[0] == 401
    fit(server, "motor")
    assert _post(_url(server, "/detectors/motor/vibration/value/detect"), {"window": _sine(50.0)}, token=None)[0] == 200
    assert _post(_url(server, "/detectors/motor/vibration/value/drift/init"), {"baselineScores": [1.0, 2.0, 1.5]}, token=None)[0] == 401


def test_protect_reads_covers_the_series_too(tmp_path: Path):
    srv = DetectorServer(("127.0.0.1", 0), _factory(), token=TOKEN, protect_reads=True, registry=DetectorRegistry(_factory))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        _post(_url(srv, "/detectors/motor/vibration/value/fit"), {"windows": [_noisy(50.0, i) for i in range(8)]})
        assert _post(_url(srv, "/detectors/motor/vibration/value/detect"), {"window": _sine(50.0)}, token=None)[0] == 401
        assert _post(_url(srv, "/detectors/motor/vibration/value/detect"), {"window": _sine(50.0)})[0] == 200
        assert _get(_url(srv, "/detectors"))[0] == 401
        assert _get(_url(srv, "/detectors"), TOKEN)[0] == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_name_that_is_not_a_series_is_400(server):
    for path in ("/detectors/a%20b/k/f/fit", "/detectors/..%2F..%2Fx/k/f/fit", "/detectors/%00/k/f/fit"):
        status, _ = _post(_url(server, path), {"windows": [_sine(50.0)] * 5})
        assert status == 400, path


def test_unknown_operations_and_short_paths_are_404(server):
    for path in ("/detectors/motor/vibration/value/delete", "/detectors/motor/vibration/fit", "/detectors/motor", "/detectors/"):
        assert _post(_url(server, path), {}, token=TOKEN)[0] == 404, path


def test_each_series_has_its_own_drift_monitor(server):
    fit(server, "motor")
    fit(server, "spindle")
    base = "/detectors/{}/vibration/value/drift/{}"
    assert _post(_url(server, base.format("motor", "init")), {"baselineScores": [1.0, 1.1, 0.9, 1.0], "windowSize": 3})[0] == 200
    # the spindle has none yet
    status, body = _post(_url(server, base.format("spindle", "observe")), {"score": 1.0})
    assert status == 409 and "not initialized" in body["error"]
    assert _post(_url(server, base.format("motor", "observe")), {"score": 1.0})[1]["status"] == "priming"


def test_a_refit_of_a_series_invalidates_only_its_own_drift_monitor(server):
    fit(server, "motor")
    fit(server, "spindle")
    base = "/detectors/{}/vibration/value/drift/{}"
    for source in ("motor", "spindle"):
        _post(_url(server, base.format(source, "init")), {"baselineScores": [1.0, 1.1, 0.9, 1.0], "windowSize": 3})
    fit(server, "motor")
    assert _post(_url(server, base.format("motor", "observe")), {"score": 1.0})[0] == 409
    assert _post(_url(server, base.format("spindle", "observe")), {"score": 1.0})[0] == 200


def test_the_listing_is_a_catalogue_of_series(server):
    fit(server, "motor")
    fit(server, "spindle", field="rms")
    rows = _get(_url(server, "/detectors"))[1]["detectors"]
    assert [(r["sourceId"], r["field"], r["fitted"]) for r in rows] == [("motor", "value", True), ("spindle", "rms", True)]


def test_the_baselines_survive_a_restart(tmp_path: Path):
    def build():
        registry = DetectorRegistry(_factory, tmp_path / "series")
        registry.restore_all()
        srv = DetectorServer(("127.0.0.1", 0), _factory(), token=TOKEN, registry=registry)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    first = build()
    try:
        assert fit(first, "motor")[0] == 200
    finally:
        first.shutdown()
        first.server_close()
    second = build()
    try:
        assert [r["sourceId"] for r in _get(_url(second, "/detectors"))[1]["detectors"]] == ["motor"]
        assert _post(_url(second, "/detectors/motor/vibration/value/detect"), {"window": _noisy(50.0, 7)}, token=None)[1]["anomalous"] is False
    finally:
        second.shutdown()
        second.server_close()


def test_the_default_routes_still_work_exactly_as_before(server):
    assert _post(_url(server, "/baseline/fit"), {"windows": [_noisy(50.0, i) for i in range(8)]})[0] == 200
    assert _post(_url(server, "/detect"), {"window": _noisy(50.0, 3)}, token=None)[1]["anomalous"] is False
    assert urllib.parse.urlparse(_url(server, "/stats")).path == "/stats"
