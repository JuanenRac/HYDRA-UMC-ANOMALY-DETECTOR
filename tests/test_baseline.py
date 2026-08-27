# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - tests/test_baseline.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
from __future__ import annotations

import numpy as np
import pytest

from hydra_umc_anomaly_detector.baseline import BaselineError, fit_baseline
from hydra_umc_anomaly_detector.fft import compute_spectrum


def _sine(freq_hz: float, sample_rate: float, n_samples: int) -> np.ndarray:
    t = np.arange(n_samples) / sample_rate
    return np.sin(2 * np.pi * freq_hz * t)


def test_fit_baseline_needs_at_least_two_windows() -> None:
    with pytest.raises(BaselineError):
        fit_baseline([_sine(50.0, 1000.0, 200)], sample_rate=1000.0)


def test_fit_baseline_rejects_mismatched_window_lengths() -> None:
    windows = [_sine(50.0, 1000.0, 200), _sine(50.0, 1000.0, 300)]
    with pytest.raises(BaselineError):
        fit_baseline(windows, sample_rate=1000.0)


def test_fit_baseline_identical_windows_give_near_zero_z_score() -> None:
    sample_rate = 1000.0
    n = 200
    windows = [_sine(50.0, sample_rate, n) for _ in range(5)]
    baseline = fit_baseline(windows, sample_rate)

    same_spectrum = compute_spectrum(windows[0], sample_rate)
    z = baseline.z_scores(same_spectrum)

    # Every window was identical, so this signal IS the mean exactly -
    # z-scores must be genuinely ~0 everywhere, not just "small".
    assert np.allclose(z, 0.0, atol=1e-6)


def test_z_scores_rejects_bin_count_mismatch() -> None:
    sample_rate = 1000.0
    windows = [_sine(50.0, sample_rate, 200) for _ in range(3)]
    baseline = fit_baseline(windows, sample_rate)

    wrong_length_spectrum = compute_spectrum(_sine(50.0, sample_rate, 400), sample_rate)
    with pytest.raises(BaselineError):
        baseline.z_scores(wrong_length_spectrum)


def test_min_std_floor_prevents_division_by_zero() -> None:
    # Every training window is bit-for-bit identical -> real std would be
    # exactly 0 in every bin without the floor, and z_scores() would
    # divide by zero the moment a live reading differs at all.
    sample_rate = 1000.0
    windows = [_sine(50.0, sample_rate, 200) for _ in range(4)]
    baseline = fit_baseline(windows, sample_rate)
    assert np.all(baseline.std > 0.0)

    different = compute_spectrum(_sine(80.0, sample_rate, 200), sample_rate)
    z = baseline.z_scores(different)
    assert np.all(np.isfinite(z))  # no inf/nan from a zero-division
