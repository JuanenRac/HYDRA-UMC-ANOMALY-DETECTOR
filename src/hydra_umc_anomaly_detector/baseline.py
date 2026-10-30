# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - src/hydra_umc_anomaly_detector/baseline.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
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

import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .fft import WINDOWS, Spectrum, compute_spectrum


# A baseline fitted from very few windows underestimates the spread of a healthy motor and raises false alarms,
# so the default asks for 10 windows and uses the sample standard deviation (ddof=1). The earlier behaviour
# (2 windows, population standard deviation) is still available as LEGACY_MIN_WINDOWS / LEGACY_DDOF.
DEFAULT_MIN_WINDOWS = 10
DEFAULT_DDOF = 1
LEGACY_MIN_WINDOWS = 2
LEGACY_DDOF = 0


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
    # The window the spectra were made with (fft.WINDOWS). A baseline saved before this was recorded used none.
    window: str = "none"

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
    window: str = "none",
    min_windows: int | None = None,
    ddof: int | None = None,
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
    # Read at call time so the defaults above are the single place that sets them.
    min_windows = DEFAULT_MIN_WINDOWS if min_windows is None else min_windows
    ddof = DEFAULT_DDOF if ddof is None else ddof
    if min_windows < 2:
        raise BaselineError(f"min_windows must be at least 2, got {min_windows}")
    if ddof not in (0, 1):
        raise BaselineError(f"ddof must be 0 or 1, got {ddof}")
    if len(healthy_windows) < min_windows:
        raise BaselineError(
            f"need at least {min_windows} healthy windows to compute a standard deviation, got {len(healthy_windows)}"
        )
    spectra = [compute_spectrum(w, sample_rate, window) for w in healthy_windows]
    lengths = {len(s.magnitudes) for s in spectra}
    if len(lengths) != 1:
        raise BaselineError(f"all healthy windows must be the same length, got spectra of sizes {sorted(lengths)}")

    magnitudes = np.stack([s.magnitudes for s in spectra])  # shape (n_windows, n_bins)
    mean = magnitudes.mean(axis=0)
    std = np.maximum(magnitudes.std(axis=0, ddof=ddof), min_std)
    return Baseline(
        freqs=spectra[0].freqs,
        mean=mean,
        std=std,
        n_windows=len(healthy_windows),
        sample_rate=float(sample_rate),
        window=window,
    )


def save_baseline(path: Path, baseline: Baseline, model_version: int) -> None:
    """Persists a fitted Baseline to disk - the fitted baseline used to live only in
    memory, so a service restart lost it and forced re-running
    POST /baseline/fit by hand. Uses numpy's own compressed `.npz` format
    (no new dependency, no home-grown binary format) - opened as a real
    file object rather than handed a bare path, since `np.savez()` would
    otherwise silently append a `.npz` suffix to a path that doesn't
    already end in one.

    The write is atomic and durable: the new file is written next to the
    target, flushed and fsynced, then renamed over it. It used to be
    written in place, so a power cut mid-write (a CM5 is power-cycled)
    left a truncated file that `load_baseline()` refused, and the last
    good baseline was lost with it; now the old file stays intact until
    the new one is complete.
    """
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with open(temporary, "wb") as fh:
            np.savez(
                fh,
                freqs=baseline.freqs,
                mean=baseline.mean,
                std=baseline.std,
                model_version=np.asarray(model_version),
                n_windows=np.asarray(baseline.n_windows),
                sample_rate=np.asarray(baseline.sample_rate),
                window=np.asarray(baseline.window),
            )
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    # make the rename itself durable (POSIX; a directory cannot be opened this way on Windows)
    try:
        directory = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory)
    except OSError:
        pass
    finally:
        os.close(directory)


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
            window = str(data["window"]) if "window" in data.files else "none"
    except (OSError, KeyError, ValueError) as exc:
        raise BaselineError(f"could not load baseline from {path}: {exc}") from exc
    if window not in WINDOWS:
        raise BaselineError(f"could not load baseline from {path}: unknown window {window!r}")
    return Baseline(freqs=freqs, mean=mean, std=std, n_windows=n_windows, sample_rate=sample_rate, window=window), model_version
