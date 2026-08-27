# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - tests/test_fft.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
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


def test_peak_frequency_on_empty_spectrum_raises() -> None:
    # Exercises Spectrum.peak_frequency()'s own guard directly - every
    # signal length compute_spectrum() actually accepts (>=2 samples)
    # yields at least one non-DC bin, so an empty Spectrum only arises
    # from constructing one directly (not a reachable compute_spectrum
    # output), which is exactly what this test does.
    empty = Spectrum(freqs=np.array([]), magnitudes=np.array([]))
    with pytest.raises(ValueError):
        empty.peak_frequency()
