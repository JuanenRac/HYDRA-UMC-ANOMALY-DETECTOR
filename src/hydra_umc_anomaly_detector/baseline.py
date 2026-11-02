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
from .robust import (
    MAD_TO_SIGMA,
    QUANTILE_RANGE_TO_SIGMA,
    MIN_TRAINING,
    MIN_VALIDATION,
    RobustSettings,
    features,
    score_features,
    split_windows,
)


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
    # "zscore" (mean and standard deviation of the magnitudes, the original) or "robust" (median and MAD of the log magnitudes normalised by the RMS,
    # robust.py). A robust baseline carries the threshold that was found with its validation windows and the settings it was made with.
    mode: str = "zscore"
    threshold: float | None = None
    settings: RobustSettings | None = None
    validation_windows: int = 0

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


def fit_robust_baseline(
    healthy_windows: list[np.ndarray] | list[list[float]],
    sample_rate: float,
    settings: RobustSettings,
    *,
    window: str = "none",
) -> Baseline:
    """The robust baseline (robust.py): the median and the scaled MAD of the log magnitudes of the training windows, and a threshold from the held-out ones."""
    if len(healthy_windows) < settings.min_windows:
        raise BaselineError(f"the robust fit needs at least {settings.min_windows} healthy windows (some are held out to find the threshold), got {len(healthy_windows)}")
    training, validation = split_windows(len(healthy_windows), settings.validation_fraction)
    if len(training) < MIN_TRAINING or len(validation) < MIN_VALIDATION:
        raise BaselineError(f"{len(healthy_windows)} windows leave {len(training)} to learn from and {len(validation)} to find the threshold; at least {MIN_TRAINING} and {MIN_VALIDATION} are needed")
    try:
        rows = [features(w, sample_rate, window, settings) for w in healthy_windows]
    except ValueError as exc:
        raise BaselineError(str(exc)) from exc
    lengths = {len(r[1]) for r in rows}
    if len(lengths) != 1:
        raise BaselineError(f"all healthy windows must be the same length, got spectra of sizes {sorted(lengths)}")
    matrix = np.stack([r[1] for r in rows])
    train = matrix[training]
    centre = np.median(train, axis=0)
    low, high = np.quantile(train, [0.1, 0.9], axis=0)
    # the larger of the MAD (blind to a bump that is there in fewer than half the windows) and the spread between the 10th and the 90th percentile
    # (which sees a bump that is there in more than one window in ten, as a peak that wanders in and out of a bin does)
    spread = np.maximum(np.maximum(np.median(np.abs(train - centre), axis=0) * MAD_TO_SIGMA, (high - low) / QUANTILE_RANGE_TO_SIGMA), settings.min_scale)
    scores = [score_features((matrix[i] - centre) / spread, settings.bands, settings.side)[0] for i in validation]
    threshold = float(np.quantile(scores, settings.quantile)) * settings.margin
    return Baseline(
        freqs=rows[0][0], mean=centre, std=spread, n_windows=len(training), sample_rate=float(sample_rate), window=window,
        mode="robust", threshold=threshold, settings=settings, validation_windows=len(validation),
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
                **_robust_arrays(baseline),  # type: ignore[arg-type]
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


def _robust_arrays(baseline: Baseline) -> dict[str, np.ndarray]:
    """What a robust baseline adds to the file: its mode, the threshold it found and every setting, so the file says by itself how to use it."""
    if baseline.mode != "robust" or baseline.settings is None or baseline.threshold is None:
        return {}
    cfg = baseline.settings
    return {
        "mode": np.asarray("robust"), "threshold": np.asarray(baseline.threshold), "validation_windows": np.asarray(baseline.validation_windows),
        "quantile": np.asarray(cfg.quantile), "margin": np.asarray(cfg.margin), "validation_fraction": np.asarray(cfg.validation_fraction),
        "bands": np.asarray(cfg.bands), "side": np.asarray(cfg.side), "normalize": np.asarray(cfg.normalize), "min_windows": np.asarray(cfg.min_windows),
        "energy_floor": np.asarray(cfg.energy_floor), "min_scale": np.asarray(cfg.min_scale),
    }


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
            mode = str(data["mode"]) if "mode" in data.files else "zscore"
            threshold = settings = None
            validation_windows = 0
            if mode == "robust":
                threshold = float(data["threshold"])
                validation_windows = int(data["validation_windows"])
                settings = RobustSettings(
                    quantile=float(data["quantile"]), margin=float(data["margin"]), validation_fraction=float(data["validation_fraction"]),
                    bands=int(data["bands"]), side=str(data["side"]) if "side" in data.files else "both", normalize=bool(data["normalize"]), min_windows=int(data["min_windows"]),
                    energy_floor=float(data["energy_floor"]), min_scale=float(data["min_scale"]),
                )
    except (OSError, KeyError, ValueError) as exc:
        raise BaselineError(f"could not load baseline from {path}: {exc}") from exc
    if window not in WINDOWS:
        raise BaselineError(f"could not load baseline from {path}: unknown window {window!r}")
    if mode not in ("zscore", "robust"):
        raise BaselineError(f"could not load baseline from {path}: unknown scoring {mode!r}")
    return Baseline(freqs=freqs, mean=mean, std=std, n_windows=n_windows, sample_rate=sample_rate, window=window, mode=mode, threshold=threshold,
                    settings=settings, validation_windows=validation_windows), model_version
