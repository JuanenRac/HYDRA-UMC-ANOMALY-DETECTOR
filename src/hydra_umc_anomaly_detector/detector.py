# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - src/hydra_umc_anomaly_detector/detector.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""AnomalyDetector: the real classical-statistics detector this v0 ships
- fit a Baseline from healthy windows, score a live window by how many
standard deviations its worst frequency bin is from that baseline.

Honest naming: the README calls this "AI-driven predictive maintenance".
What's real and shipping today is a legitimate, real signal-processing/
statistics technique (FFT + per-bin z-score against a learned healthy
baseline) - not a trained neural network. It works, it's testable, and
it's the right foundation to build a learned model on top of later - a
trained model needs real labeled field data to train and validate
against, which doesn't exist yet, so that step is deliberately deferred
future work rather than attempted with synthetic data alone in this
first pass - but calling it "AI" here would overstate what's actually
running.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .baseline import Baseline, BaselineError, fit_baseline
from .fft import WINDOWS, compute_spectrum


class NotFittedError(RuntimeError):
    """Raised by score()/is_anomalous() before fit() has ever succeeded -
    a real guard, not a silent "everything looks fine" default."""


@dataclass(frozen=True)
class Verdict:
    score: float
    anomalous: bool
    worst_bin_freq: float
    model_version: int
    threshold: float
    calibration_windows: int = 0


class AnomalyDetector:
    def __init__(self, sample_rate: float, *, threshold: float = 10.0, window: str = "none") -> None:
        """`threshold` is a cutoff on the WORST (max) per-bin z-score
        across the whole spectrum - taking a max over many bins (a
        500-sample window has ~250) means even a genuinely healthy
        signal's max-z naturally runs higher than a single bin's own
        "4 sigma is rare" intuition would suggest, especially with a
        modest number of training windows (per-bin std itself is
        noisily estimated from few samples). Checked ONLY against this
        project's own synthetic healthy-vs-faulty test fixtures (see
        tests/test_detector.py): a fixed amplitude, a fixed phase and a
        frequency that falls exactly on a bin. With as few as 10 training
        windows, healthy readings scored up to ~5.3 there, while readings
        carrying a synthetic fault component scored in the hundreds. Real
        machines vary their speed, load (amplitude) and phase, none of which
        the fixtures vary, so on real data the margin can disappear: the
        threshold must be recalibrated against healthy field recordings
        before any alarm is trusted. It is not claimed to be the right
        number for any real motor.
        """
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if window not in WINDOWS:
            raise ValueError(f"window must be one of {WINDOWS}, not {window!r}")
        self._sample_rate = sample_rate
        self._window = window
        self._threshold = threshold
        self._baseline: Baseline | None = None
        # 0 means "never fitted" (is_fitted is False) - real, monotonic
        # versioning: every successful fit() is a real, distinct model a
        # Verdict can be traced back to, so a caller can tell "this
        # verdict came from the baseline fit before the motor was
        # serviced" from "after".
        self._model_version = 0

    def fit(self, healthy_windows: list[np.ndarray] | list[list[float]]) -> None:
        self.adopt(self.prepare_fit(healthy_windows))

    def prepare_fit(self, healthy_windows: list[np.ndarray] | list[list[float]]) -> Baseline:
        """Computes a new baseline WITHOUT installing it, so a caller that
        shares this detector between threads can do the heavy FFT work
        outside its lock and only swap the result in (`adopt`) inside it."""
        return fit_baseline(healthy_windows, self._sample_rate, window=self._window)

    def adopt(self, baseline: Baseline) -> None:
        """Installs a baseline from `prepare_fit` as the next model version."""
        self._baseline = baseline
        self._model_version += 1

    def restore(self, baseline: Baseline, model_version: int) -> None:
        """Restores a previously-fit, persisted `Baseline` exactly as it
        was - the fitted baseline used to live only in memory, so a real
        service restart lost it. Unlike `fit()`, this does NOT increment
        `model_version` - it's the SAME real model that already existed
        before the restart, not a newly-fit one, so a `Verdict` from
        before and after a restart still traces back to the same version
        number."""
        if model_version < 0:
            raise ValueError(f"model_version must not be negative, got {model_version}")
        # A baseline records the sampling rate it was fitted at (0 = unknown, an
        # older file). Restoring it under another rate would compare bins that
        # do not correspond and report a false frequency.
        if baseline.sample_rate and abs(baseline.sample_rate - self._sample_rate) > 1e-9:
            raise BaselineError(
                f"the baseline was fitted at {baseline.sample_rate:g} Hz but this detector samples at "
                f"{self._sample_rate:g} Hz; start with --sample-rate {baseline.sample_rate:g} or refit"
            )
        # The same for the window: the numbers of a Hann window and of no window are not comparable.
        if baseline.window != self._window:
            raise BaselineError(
                f"the baseline was made with window {baseline.window!r} but this detector uses {self._window!r}; "
                f"start with --window {baseline.window} or refit"
            )
        self._baseline = baseline
        self._model_version = model_version

    @property
    def window(self) -> str:
        return self._window

    @property
    def sample_rate(self) -> float:
        return self._sample_rate

    @property
    def baseline(self) -> Baseline | None:
        return self._baseline

    @property
    def is_fitted(self) -> bool:
        return self._baseline is not None

    @property
    def model_version(self) -> int:
        """The real, monotonic version of the currently-fitted baseline -
        0 before the first real fit() call, incremented on every real
        refit thereafter."""
        return self._model_version

    @property
    def threshold(self) -> float:
        return self._threshold

    def score(self, window: np.ndarray | list[float]) -> Verdict:
        """Scores one live signal window against the fitted baseline.
        The score is the max absolute z-score across every frequency
        bin - the worst single bin drives the verdict, matching how a
        real fault (e.g. one new peak at a bearing defect frequency)
        shows up: as an outlier in a FEW bins, not a uniform shift
        across the whole spectrum.
        """
        if self._baseline is None:
            raise NotFittedError("call fit() with known-healthy windows before score()")
        spectrum = compute_spectrum(window, self._sample_rate, self._window)
        z = self._baseline.z_scores(spectrum)
        worst_idx = int(np.argmax(np.abs(z)))
        worst_score = float(np.abs(z[worst_idx]))
        return Verdict(
            score=worst_score,
            anomalous=worst_score > self._threshold,
            worst_bin_freq=float(self._baseline.freqs[worst_idx]),
            model_version=self._model_version,
            threshold=self._threshold,
            calibration_windows=self._baseline.n_windows,
        )
