# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - tests/test_fft.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
from __future__ import annotations

import numpy as np
import pytest

from hydra_umc_anomaly_detector.fft import Spectrum, compute_spectrum


def _sine(freq_hz: float, sample_rate: float, n_samples: int, amplitude: float = 1.0) -> np.ndarray:
    t = np.arange(n_samples) / sample_rate
    return amplitude * np.sin(2 * np.pi * freq_hz * t)


def test_compute_spectrum_finds_known_sine_peak() -> None:
    sample_rate = 1000.0
    n = 1000  # 1 second -> 1 Hz frequency resolution
    signal = _sine(freq_hz=50.0, sample_rate=sample_rate, n_samples=n)

    spectrum = compute_spectrum(signal, sample_rate)

    assert spectrum.peak_frequency() == pytest.approx(50.0, abs=1.0)


def test_compute_spectrum_drops_dc_even_with_large_offset() -> None:
    sample_rate = 1000.0
    n = 1000
    # A large constant offset (would dominate a spectrum that kept DC)
    # plus a much smaller real oscillation at 30 Hz.
    signal = 1000.0 + _sine(freq_hz=30.0, sample_rate=sample_rate, n_samples=n, amplitude=1.0)

    spectrum = compute_spectrum(signal, sample_rate)

    assert 0.0 not in spectrum.freqs  # DC bin genuinely excluded
    assert spectrum.peak_frequency() == pytest.approx(30.0, abs=1.0)


def test_compute_spectrum_rejects_short_signal() -> None:
    with pytest.raises(ValueError):
        compute_spectrum([1.0], sample_rate=1000.0)


def test_compute_spectrum_rejects_non_positive_sample_rate() -> None:
    with pytest.raises(ValueError):
        compute_spectrum([1.0, 2.0, 3.0], sample_rate=0.0)


def test_compute_spectrum_rejects_a_non_finite_sample() -> None:
    # json.loads accepts the non-standard NaN/Infinity/-Infinity tokens
    # by default, so a client (or a corrupted telemetry replay) can put
    # one straight into the "window" the HTTP API hands to score(). A
    # single NaN sample propagates into EVERY bin of rfft's output, and
    # every downstream comparison against a NaN score is False - the
    # detector would silently report "not anomalous" instead of failing.
    with pytest.raises(ValueError):
        compute_spectrum([1.0, float("nan"), 3.0], sample_rate=1000.0)
    with pytest.raises(ValueError):
        compute_spectrum([1.0, float("inf"), 3.0], sample_rate=1000.0)


def test_peak_frequency_on_empty_spectrum_raises() -> None:
    # Exercises Spectrum.peak_frequency()'s own guard directly - every
    # signal length compute_spectrum() actually accepts (>=2 samples)
    # yields at least one non-DC bin, so an empty Spectrum only arises
    # from constructing one directly (not a reachable compute_spectrum
    # output), which is exactly what this test does.
    empty = Spectrum(freqs=np.array([]), magnitudes=np.array([]))
    with pytest.raises(ValueError):
        empty.peak_frequency()


# ---- Welch averaging


def test_welch_finds_the_peak_and_reports_its_amplitude() -> None:
    signal = _sine(freq_hz=50.0, sample_rate=1000.0, n_samples=1000, amplitude=2.0)
    spectrum = compute_spectrum(signal, 1000.0, "welch")
    assert spectrum.peak_frequency() == pytest.approx(50.0, abs=2.5)  # 500-sample segments: 2 Hz bins
    assert float(spectrum.magnitudes.max()) == pytest.approx(2.0, rel=0.15)
    assert len(spectrum.magnitudes) == 500 // 2


def test_welch_makes_the_noise_floor_steadier_than_one_hann_window() -> None:
    rng = np.random.default_rng(7)
    hann_spread, welch_spread = [], []
    for _ in range(40):
        noise = rng.normal(size=1000)
        hann = compute_spectrum(noise, 1000.0, "hann").magnitudes
        welch = compute_spectrum(noise, 1000.0, "welch").magnitudes
        hann_spread.append(hann.std() / hann.mean())
        welch_spread.append(welch.std() / welch.mean())
    assert np.mean(welch_spread) < 0.7 * np.mean(hann_spread)


def test_welch_needs_enough_samples() -> None:
    with pytest.raises(ValueError, match="welch"):
        compute_spectrum([0.0, 1.0] * 5, 100.0, "welch")


def test_welch_baseline_round_trips_and_a_detector_with_another_window_refuses_it(tmp_path) -> None:
    from hydra_umc_anomaly_detector.baseline import fit_baseline, load_baseline, save_baseline
    from hydra_umc_anomaly_detector.detector import AnomalyDetector

    rng = np.random.default_rng(3)
    windows = [_sine(50.0, 1000.0, 1000) + 0.05 * rng.normal(size=1000) for _ in range(6)]
    baseline = fit_baseline(windows, 1000.0, window="welch")
    assert baseline.window == "welch"
    path = tmp_path / "welch.npz"
    save_baseline(path, baseline, 1)
    loaded, _ = load_baseline(path)
    assert loaded.window == "welch" and len(loaded.mean) == 250

    detector = AnomalyDetector(sample_rate=1000.0, window="welch")
    detector.fit(windows)
    healthy = detector.score(_sine(50.0, 1000.0, 1000) + 0.05 * rng.normal(size=1000))
    faulty = detector.score(_sine(50.0, 1000.0, 1000) + _sine(180.0, 1000.0, 1000, 0.5))
    assert not healthy.anomalous and faulty.anomalous
    with pytest.raises(Exception, match="welch"):
        AnomalyDetector(sample_rate=1000.0, window="none").restore(baseline, 1)
