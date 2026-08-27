# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - src/hydra_umc_anomaly_detector/baseline.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""A Baseline is the real per-frequency-bin statistical profile of a
"healthy" motor, learned from a set of known-good signal windows - the
real reference every live reading gets compared against in detector.py.
Real, computed statistics (mean/std per bin via numpy), not a fixed
guessed threshold - a different motor, mount, or duty cycle genuinely
has a different healthy spectrum, so this has to be fit per source
rather than hardcoded.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .fft import Spectrum, compute_spectrum


class BaselineError(ValueError):
    """Raised when a Baseline can't legitimately be built or applied -
    e.g. too little training data, or a spectrum shape mismatch."""


@dataclass(frozen=True)
class Baseline:
    freqs: np.ndarray
    mean: np.ndarray
    std: np.ndarray

    def z_scores(self, spectrum: Spectrum) -> np.ndarray:
        """Per-bin z-scores of `spectrum` against this baseline - the
        real building block detector.py's scoring uses. Requires the
        same bin count this baseline was fit with (same window length +
        sample rate), otherwise the bins wouldn't correspond to the same
        frequencies and comparing them would be meaningless - raised as
        a real error, not silently truncated/padded.
        """
        if len(spectrum.magnitudes) != len(self.mean):
            raise BaselineError(
                f"spectrum has {len(spectrum.magnitudes)} bins, "
                f"baseline was fit with {len(self.mean)} - use the same "
                f"window length and sample rate as training"
            )
        return (spectrum.magnitudes - self.mean) / self.std


def fit_baseline(
    healthy_windows: list[np.ndarray] | list[list[float]],
    sample_rate: float,
    *,
    min_std: float = 1e-6,
) -> Baseline:
    """Builds a Baseline from real "known-healthy" signal windows -
    computes a real FFT per window, then the real per-bin mean/std
    across all of them.

    `min_std` floors the standard deviation of every bin (default a
    small positive epsilon, not zero) - a bin that happens to be exactly
    constant across every training window would otherwise divide by
    zero the moment a live reading differs from it at all, which is a
    real numerical failure mode this floor exists specifically to avoid.
    """
    if len(healthy_windows) < 2:
        raise BaselineError(
            f"need at least 2 healthy windows to compute a standard deviation, got {len(healthy_windows)}"
        )
    spectra = [compute_spectrum(w, sample_rate) for w in healthy_windows]
    lengths = {len(s.magnitudes) for s in spectra}
    if len(lengths) != 1:
        raise BaselineError(f"all healthy windows must be the same length, got spectra of sizes {sorted(lengths)}")

    magnitudes = np.stack([s.magnitudes for s in spectra])  # shape (n_windows, n_bins)
    mean = magnitudes.mean(axis=0)
    std = np.maximum(magnitudes.std(axis=0), min_std)
    return Baseline(freqs=spectra[0].freqs, mean=mean, std=std)
