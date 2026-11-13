# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - tests/test_spectrogram.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
from __future__ import annotations

import numpy as np
import pytest

from hydra_umc_anomaly_detector.spectrogram import compute_spectrogram, find_transients

RATE = 2000.0


def _signal(seconds: float = 2.0, seed: int = 7) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    noise = np.random.default_rng(seed).normal(0.0, 0.05, len(t))
    return np.sin(2 * np.pi * 100.0 * t) + noise


def test_shape_times_and_peak_frequency() -> None:
    spec = compute_spectrogram(_signal(), RATE, frame=256)
    assert spec.magnitudes.shape == (len(spec.times), len(spec.freqs))
    assert spec.times[1] - spec.times[0] == pytest.approx(128 / RATE)
    peaks = spec.freqs[np.argmax(spec.magnitudes, axis=1)]
    assert np.all(np.abs(peaks - 100.0) < 2 * RATE / 256)


def test_hop_sets_the_number_of_frames() -> None:
    data = np.arange(1000) % 7
    assert len(compute_spectrogram(data, RATE, frame=100, hop=100).times) == 10
    assert len(compute_spectrogram(data, RATE, frame=100, hop=50).times) == 19


@pytest.mark.parametrize("kwargs", [{"frame": 4}, {"frame": 64, "hop": 0}, {"frame": 5000}])
def test_bad_framing_is_refused(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        compute_spectrogram(_signal(), RATE, **kwargs)


def test_non_finite_and_bad_rate_are_refused() -> None:
    bad = _signal()
    bad[10] = float("nan")
    with pytest.raises(ValueError):
        compute_spectrogram(bad, RATE)
    with pytest.raises(ValueError):
        compute_spectrogram(_signal(), 0.0)
    with pytest.raises(ValueError):
        compute_spectrogram(np.zeros((4, 4)), RATE)


def test_a_burst_is_found_in_the_frames_where_it_happens() -> None:
    data = _signal()
    t = np.arange(len(data)) / RATE
    burst = slice(2000, 2060)
    data[burst] += 3.0 * np.sin(2 * np.pi * 600.0 * t[burst])
    spec = compute_spectrogram(data, RATE, frame=128)
    found = find_transients(spec.band_energy(500.0, 700.0))
    assert found
    # the burst is between 1.000 s and 1.030 s: every flagged frame overlaps it
    assert all(1.0 - 128 / RATE <= spec.times[i] <= 1.03 for i in found)


def test_a_steady_signal_has_no_transient() -> None:
    spec = compute_spectrogram(_signal(), RATE, frame=128)
    assert find_transients(spec.frame_energy()) == []


def test_equal_energy_frames_give_no_alarm_instead_of_a_flood() -> None:
    assert find_transients([1.0] * 20) == []
    assert find_transients([1.0] * 20 + [50.0]) == []


def test_find_transients_edges() -> None:
    assert find_transients([1.0, 2.0]) == []
    assert find_transients([1.0, float("nan"), 2.0, 3.0]) == []
    assert find_transients([-1.0, 2.0, 3.0, 4.0]) == []
    with pytest.raises(ValueError):
        find_transients([1.0, 2.0, 3.0], threshold=0.0)
    with pytest.raises(ValueError):
        find_transients(np.ones((3, 3)))


def test_band_energy_needs_an_ordered_band() -> None:
    spec = compute_spectrogram(_signal(), RATE, frame=128)
    with pytest.raises(ValueError):
        spec.band_energy(300.0, 300.0)
    assert spec.band_energy(50.0, 150.0).sum() > spec.band_energy(400.0, 500.0).sum()
