# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - tests/test_window_token_fetch.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The Hann window (and the baseline that remembers it), the bearer token for the writing routes, and the
timestamp-checked, paged read of the training history."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator

import numpy as np
import pytest

from fake_datalake import running_fake_datalake
from hydra_umc_anomaly_detector.api import DetectorServer
from hydra_umc_anomaly_detector.baseline import BaselineError, fit_baseline, load_baseline, save_baseline
from hydra_umc_anomaly_detector.datalake_client import DatalakeClient, fetch_windows_checked
from hydra_umc_anomaly_detector.detector import AnomalyDetector
from hydra_umc_anomaly_detector.fft import compute_spectrum
from hydra_umc_anomaly_detector.main import main

RATE = 1000.0


# ---------------------------------------------------------------------------- the window
def _sine(freq: float, amplitude: float = 1.0, n: int = 500) -> np.ndarray:
    return amplitude * np.sin(2 * np.pi * freq * np.arange(n) / RATE)


def test_the_default_window_is_the_original_behaviour() -> None:
    signal = _sine(50.3)
    assert np.array_equal(compute_spectrum(signal, RATE).magnitudes, compute_spectrum(signal, RATE, "none").magnitudes)
    with pytest.raises(ValueError):
        compute_spectrum(signal, RATE, "blackman")


def test_hann_leaks_far_less_than_no_window_for_a_frequency_between_bins() -> None:
    signal = _sine(50.3)  # 500 samples at 1 kHz: 2 Hz bins, so 50.3 Hz is not on a bin
    plain, hann = compute_spectrum(signal, RATE), compute_spectrum(signal, RATE, "hann")

    def far_fraction(spectrum) -> float:
        peak = int(np.argmax(spectrum.magnitudes))
        mask = np.abs(np.arange(len(spectrum.magnitudes)) - peak) > 4
        return float(spectrum.magnitudes[mask].sum() / spectrum.magnitudes.sum())

    assert far_fraction(hann) < far_fraction(plain) / 10


def test_hann_scales_a_sine_to_about_its_amplitude_and_ignores_an_offset() -> None:
    spectrum = compute_spectrum(_sine(50.0, amplitude=3.0) + 100.0, RATE, "hann")
    assert spectrum.peak_frequency() == pytest.approx(50.0, abs=2.0)
    assert float(spectrum.magnitudes.max()) == pytest.approx(3.0, rel=0.05)


def test_a_baseline_remembers_its_window_through_a_save(tmp_path) -> None:
    rng = np.random.default_rng(1)
    windows = [_sine(50.0) + 0.05 * rng.standard_normal(500) for _ in range(6)]
    baseline = fit_baseline(windows, RATE, window="hann")
    assert baseline.window == "hann"
    path = tmp_path / "b.npz"
    save_baseline(path, baseline, 3)
    loaded, version = load_baseline(path)
    assert (loaded.window, version) == ("hann", 3)


def test_a_baseline_saved_before_windows_existed_loads_as_none(tmp_path) -> None:
    path = tmp_path / "old.npz"
    baseline = fit_baseline([_sine(50.0) + 0.01 * i for i in range(3)], RATE)
    with open(path, "wb") as handle:  # the file as the older version wrote it: no `window` array
        np.savez(handle, freqs=baseline.freqs, mean=baseline.mean, std=baseline.std, model_version=np.asarray(1))
    loaded, _ = load_baseline(path)
    assert loaded.window == "none"


def test_a_detector_refuses_to_restore_a_baseline_made_with_another_window() -> None:
    windows = [_sine(50.0) + 0.02 * np.random.default_rng(i).standard_normal(500) for i in range(5)]
    made_with_hann = AnomalyDetector(RATE, window="hann")
    made_with_hann.fit(windows)
    plain = AnomalyDetector(RATE)
    with pytest.raises(BaselineError, match="window"):
        plain.restore(made_with_hann.baseline, 1)
    again = AnomalyDetector(RATE, window="hann")
    again.restore(made_with_hann.baseline, 1)
    assert again.is_fitted


def test_the_hann_detector_still_tells_a_fault_from_a_healthy_window() -> None:
    from signal_fixtures import SAMPLE_RATE, faulty_signal, healthy_signal

    rng = np.random.default_rng(7)
    detector = AnomalyDetector(SAMPLE_RATE, threshold=10.0, window="hann")
    detector.fit([healthy_signal(rng) for _ in range(20)])
    assert not detector.score(healthy_signal(rng)).anomalous
    assert detector.score(faulty_signal(rng)).anomalous


# ---------------------------------------------------------------------------- the token
@pytest.fixture()
def protected_server() -> Iterator[str]:
    server = DetectorServer(("127.0.0.1", 0), AnomalyDetector(RATE, threshold=4.0), token="a-long-enough-secret-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _post(url: str, body: dict, token: str | None = None) -> int:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as error:
        error.read()
        return error.code


def test_the_writing_routes_need_the_token_and_the_reading_ones_do_not(protected_server: str) -> None:
    windows = {"windows": [list(_sine(50.0, n=200) + 0.01 * i) for i in range(4)]}
    assert _post(f"{protected_server}/baseline/fit", windows) == 401
    assert _post(f"{protected_server}/baseline/fit", windows, "wrong-token-of-the-same-length") == 401
    assert _post(f"{protected_server}/drift/init", {"baselineScores": [1.0, 2.0]}) == 401
    assert _post(f"{protected_server}/drift/observe", {"score": 1.0}) == 401
    assert _post(f"{protected_server}/baseline/fit", windows, "a-long-enough-secret-token") == 200
    # reading: detect is open (it changes nothing), and so is /stats
    assert _post(f"{protected_server}/detect", {"window": list(_sine(50.0, n=200))}) == 200
    with urllib.request.urlopen(f"{protected_server}/stats", timeout=5) as resp:
        body = json.loads(resp.read())
    assert body["writeProtected"] is True


def test_the_command_refuses_an_open_address_without_a_token_and_a_short_token(capsys, monkeypatch) -> None:
    monkeypatch.delenv("HYDRA_UMC_ANOMALY_TOKEN", raising=False)
    assert main(["--addr", "0.0.0.0", "--port", "0"]) == 2
    assert "not loopback" in capsys.readouterr().err
    monkeypatch.setenv("HYDRA_UMC_ANOMALY_TOKEN", "short")
    assert main(["--addr", "127.0.0.1", "--port", "0"]) == 2
    assert "16 characters" in capsys.readouterr().err
    assert main(["--addr", "127.0.0.1", "--token-file", "/no/such/file"]) == 2


# ---------------------------------------------------------------------------- the checked read
def _series(start: int, count: int, step: int = 1, value=lambda i: float(i)) -> list[dict]:
    return [
        {"sourceId": "m", "kind": "vib", "field": "v", "timestamp": start + i * step, "value": value(i)}
        for i in range(count)
    ]


def _fetch(url: str, **extra):
    client = DatalakeClient(url)
    return fetch_windows_checked(client, source_id="m", kind="vib", field="v", window_size=10, sample_rate=1000.0, **extra)


def test_an_even_series_gives_every_window_and_pages_through_the_history() -> None:
    with running_fake_datalake() as (url, server):
        server.points = _series(1_000_000, 1005)
        result = _fetch(url, page_size=100)
        assert result.pages == 11 and result.points == 1005 and result.gaps == 0 and not result.truncated
        assert len(result.windows) == 100 and result.dropped_samples == 5
        assert result.windows[0] == [float(i) for i in range(10)]


def test_a_gap_cuts_the_series_and_no_window_spans_it() -> None:
    with running_fake_datalake() as (url, server):
        server.points = _series(0, 25) + _series(10_000, 25, value=lambda i: 1000.0 + i)
        result = _fetch(url)
        assert result.gaps == 1
        assert len(result.windows) == 4  # 2 + 2: 25 points make 2 windows and 5 left over, in each run
        assert result.dropped_samples == 10
        assert all(max(w) < 100 or min(w) >= 1000 for w in result.windows), "a window mixes the two sides of the gap"


def test_a_wandering_interval_is_cut_too_and_repeated_timestamps_are_removed() -> None:
    with running_fake_datalake() as (url, server):
        points = _series(0, 20)
        points.insert(6, dict(points[5], value=999.0))  # the same timestamp again, after the first
        points += _series(20, 20, step=3)  # a series that slowed down to a third
        server.points = points
        result = _fetch(url)
        assert result.duplicates == 1
        assert result.gaps >= 1
        assert len(result.windows) == 2 and all(999.0 not in w for w in result.windows)


def test_reaching_the_point_limit_is_reported_not_silent() -> None:
    with running_fake_datalake() as (url, server):
        server.points = _series(0, 500)
        result = _fetch(url, page_size=100, max_points=200)
        assert result.truncated and result.points == 200
        assert "STOPPED" in result.summary()


def test_the_training_range_is_asked_for_and_bad_arguments_are_refused() -> None:
    with running_fake_datalake() as (url, server):
        server.points = _series(0, 100)
        result = _fetch(url, start=50, end=89)
        assert result.points == 40 and len(result.windows) == 4
        for bad in ({"window_size": 0}, {"sample_rate": 0.0}, {"page_size": 0}, {"tolerance": 1.0}):
            args = {"window_size": 10, "sample_rate": 1000.0, **bad}
            with pytest.raises(ValueError):
                fetch_windows_checked(DatalakeClient(url), source_id="m", kind="vib", field="v", **args)
