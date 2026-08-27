# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - src/hydra_umc_anomaly_detector/detector.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""AnomalyDetector: the real classical-statistics detector this v0 ships
- fit a Baseline from healthy windows, score a live window by how many
standard deviations its worst frequency bin is from that baseline.

Honest naming: the README calls this "AI-driven predictive maintenance".
What's real and shipping today is a legitimate, real signal-processing/
statistics technique (FFT + per-bin z-score against a learned healthy
baseline) - not a trained neural network. It works, it's testable, and
it's the right foundation to build a learned model on top of later (see
mejoras_futuras.txt for why that's deliberately not attempted in this
first pass) - but calling it "AI" here would overstate what's actually
running.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .baseline import Baseline, fit_baseline
from .fft import compute_spectrum


class NotFittedError(RuntimeError):
    """Raised by score()/is_anomalous() before fit() has ever succeeded -
    a real guard, not a silent "everything looks fine" default."""


@dataclass(frozen=True)
class Verdict:
    score: float
    anomalous: bool
    worst_bin_freq: float


class AnomalyDetector:
    def __init__(self, sample_rate: float, *, threshold: float = 10.0) -> None:
        """`threshold` is a cutoff on the WORST (max) per-bin z-score
        across the whole spectrum - taking a max over many bins (a
        500-sample window has ~250) means even a genuinely healthy
        signal's max-z naturally runs higher than a single bin's own
        "4 sigma is rare" intuition would suggest, especially with a
        modest number of training windows (per-bin std itself is
        noisily estimated from few samples). Empirically verified against
        this project's own synthetic healthy-vs-faulty test fixtures
        (see tests/test_detector.py): with as few as 10 training windows,
        genuinely healthy readings scored up to ~5.3, while a readings
        carrying a real synthetic fault component scored in the
        hundreds - a real, wide margin, not a knife-edge tuning. 10.0 sits
        safely above the observed healthy ceiling with room to spare.
        Tunable per deployment once there's real field data to tune it
        against (see mejoras_futuras.txt) - not claimed to be the
        universally correct number for every motor.
        """
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        self._sample_rate = sample_rate
        self._threshold = threshold
        self._baseline: Baseline | None = None

    def fit(self, healthy_windows: list[np.ndarray] | list[list[float]]) -> None:
        self._baseline = fit_baseline(healthy_windows, self._sample_rate)

    @property
    def is_fitted(self) -> bool:
        return self._baseline is not None

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
        spectrum = compute_spectrum(window, self._sample_rate)
        z = self._baseline.z_scores(spectrum)
        worst_idx = int(np.argmax(np.abs(z)))
        worst_score = float(np.abs(z[worst_idx]))
        return Verdict(
            score=worst_score,
            anomalous=worst_score > self._threshold,
            worst_bin_freq=float(self._baseline.freqs[worst_idx]),
        )
