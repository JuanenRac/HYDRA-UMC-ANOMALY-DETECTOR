# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - src/hydra_umc_anomaly_detector/fft.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""Real FFT-based feature extraction - the actual signal-processing step
the README names ("FFT/spectrogram analysis of high-frequency
telemetry"), backed by numpy's own FFT implementation rather than a
hand-rolled DFT. Motor vibration/current signatures are exactly the kind
of periodic signal an FFT is the standard tool for: a worn bearing or a
loose belt shows up as new/shifted frequency peaks, not as a shift in
the raw time-domain average - which is exactly why this project doesn't
just threshold the raw signal.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Spectrum:
    """The result of one FFT: parallel arrays of frequency (Hz) and
    magnitude, DC component excluded (see compute_spectrum)."""

    freqs: np.ndarray
    magnitudes: np.ndarray

    def peak_frequency(self) -> float:
        """The frequency with the largest magnitude - the real, testable
        signature of "what's this signal actually oscillating at",
        e.g. a motor's dominant vibration frequency."""
        if len(self.magnitudes) == 0:
            raise ValueError("cannot find a peak in an empty spectrum")
        return float(self.freqs[int(np.argmax(self.magnitudes))])


def compute_spectrum(signal: np.ndarray | list[float], sample_rate: float) -> Spectrum:
    """Computes the real (one-sided) amplitude spectrum of `signal`,
    sampled at `sample_rate` Hz, using numpy's real-input FFT
    (``numpy.fft.rfft`` - about 2x cheaper than a full complex FFT for a
    real-valued signal, which vibration/current telemetry always is).

    The DC bin (0 Hz) is dropped: for vibration/current analysis the
    interesting signal is in the oscillation, not the average level -
    keeping DC in would make it the "peak" of most real, real-world
    signals and drown out the actual frequency content this project
    cares about.
    """
    signal = np.asarray(signal, dtype=np.float64)
    if signal.ndim != 1:
        raise ValueError("signal must be 1-D")
    if len(signal) < 2:
        raise ValueError("signal must have at least 2 samples")
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")

    spectrum = np.fft.rfft(signal)
    freqs = np.fft.rfftfreq(len(signal), d=1.0 / sample_rate)
    magnitudes = np.abs(spectrum)

    # Drop the DC bin (index 0) - see docstring.
    return Spectrum(freqs=freqs[1:], magnitudes=magnitudes[1:])
