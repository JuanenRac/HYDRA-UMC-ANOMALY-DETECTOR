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
from pathlib import Path

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
    # Calibration provenance: how many healthy windows this baseline was
    # fit from and at what sample rate. 0 means "unknown" (a baseline
    # persisted before this was recorded), never a guess.
    n_windows: int = 0
    sample_rate: float = 0.0

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
    return Baseline(
        freqs=spectra[0].freqs,
        mean=mean,
        std=std,
        n_windows=len(healthy_windows),
        sample_rate=float(sample_rate),
    )


def save_baseline(path: Path, baseline: Baseline, model_version: int) -> None:
    """Persists a fitted Baseline to disk - the fitted baseline used to live only in
    memory, so a service restart lost it and forced re-running
    POST /baseline/fit by hand. Uses numpy's own compressed `.npz` format
    (no new dependency, no home-grown binary format) - opened as a real
    file object rather than handed a bare path, since `np.savez()` would
    otherwise silently append a `.npz` suffix to a path that doesn't
    already end in one.
    """
    with open(path, "wb") as fh:
        np.savez(
            fh,
            freqs=baseline.freqs,
            mean=baseline.mean,
            std=baseline.std,
            model_version=np.asarray(model_version),
            n_windows=np.asarray(baseline.n_windows),
            sample_rate=np.asarray(baseline.sample_rate),
        )


def load_baseline(path: Path) -> tuple[Baseline, int]:
    """The real counterpart to `save_baseline()`. Raises `BaselineError`
    for anything that isn't a real, previously-saved baseline (missing
    file, corrupt/foreign `.npz`, a missing array) rather than silently
    starting unfitted, which would look identical to "nobody ever called
    fit()" - a real, different, and worse condition to mistake this for.
    """
    try:
        with open(path, "rb") as fh, np.load(fh) as data:
            freqs = data["freqs"]
            mean = data["mean"]
            std = data["std"]
            model_version = int(data["model_version"])
            # Absent in files saved before provenance was recorded.
            n_windows = int(data["n_windows"]) if "n_windows" in data.files else 0
            sample_rate = float(data["sample_rate"]) if "sample_rate" in data.files else 0.0
    except (OSError, KeyError, ValueError) as exc:
        raise BaselineError(f"could not load baseline from {path}: {exc}") from exc
    return Baseline(freqs=freqs, mean=mean, std=std, n_windows=n_windows, sample_rate=sample_rate), model_version
