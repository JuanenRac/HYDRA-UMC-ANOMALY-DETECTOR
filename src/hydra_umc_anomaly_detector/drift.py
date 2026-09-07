# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - src/hydra_umc_anomaly_detector/drift.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""Real drift detection - a different real question from detector.py's
own "is THIS one window anomalous": has the RECENT distribution of
scores quietly shifted away from what the fitted baseline's own
training data looked like. Real, slow degradation (a loosening mount, a
wearing bearing) can raise every reading's score a little without any
single one crossing the anomaly threshold - DriftMonitor is the real,
separate, additive mechanism that would still catch that.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass


class DriftMonitorError(ValueError):
    """Raised when a DriftMonitor can't legitimately be built - e.g. too
    little baseline data, or a non-positive baseline mean score."""


@dataclass(frozen=True)
class DriftReport:
    baseline_mean_score: float
    recent_mean_score: float
    drift_ratio: float
    drifted: bool


class DriftMonitor:
    """Tracks a real rolling window of recent `Verdict.score` values and
    compares their mean against a real, fixed reference established once
    from the baseline's own known-healthy scores."""

    def __init__(
        self,
        baseline_scores: list[float],
        *,
        window_size: int = 20,
        drift_ratio_threshold: float = 2.0,
    ) -> None:
        if len(baseline_scores) < 2:
            raise DriftMonitorError(
                f"need at least 2 baseline scores to establish a reference, got {len(baseline_scores)}"
            )
        # NaN/Infinity compare False against every real number (NaN <= 0
        # is False, so is NaN > 0), so a poisoned baseline would sail
        # straight past the "must be positive" guard below and leave
        # every future observe() computing ratio = recent_mean / NaN -
        # permanently NaN, which then compares False against the drift
        # threshold forever: drift detection silently, permanently
        # disabled by a single bad sample. Reject at construction instead.
        if not all(math.isfinite(s) for s in baseline_scores):
            raise DriftMonitorError("baseline_scores must all be finite numbers (no NaN/Infinity)")
        self._baseline_mean = sum(baseline_scores) / len(baseline_scores)
        if self._baseline_mean <= 0:
            raise DriftMonitorError(f"baseline mean score must be positive, got {self._baseline_mean}")
        if window_size < 1:
            raise DriftMonitorError(f"window_size must be positive, got {window_size}")
        if not math.isfinite(drift_ratio_threshold) or drift_ratio_threshold <= 1.0:
            raise DriftMonitorError(
                f"drift_ratio_threshold must be a finite number > 1.0 (a rolling mean at or below "
                f"the real baseline is never drift), got {drift_ratio_threshold}"
            )
        self._window_size = window_size
        self._drift_ratio_threshold = drift_ratio_threshold
        self._recent: deque[float] = deque(maxlen=window_size)

    @property
    def baseline_mean_score(self) -> float:
        return self._baseline_mean

    def observe(self, score: float) -> DriftReport | None:
        """Real, incremental drift check - records `score`, and only once
        at least `window_size` real recent scores have accumulated,
        reports whether their real rolling mean has drifted past
        `drift_ratio_threshold` times the real baseline mean. Returns
        `None` before enough recent data exists - an honest "not enough
        evidence yet", never a premature verdict from a half-full window.
        """
        if not math.isfinite(score):
            # Same fail-open risk as the constructor guard above, but
            # worse here: one bad `score` poisons the rolling window and
            # the resulting NaN mean/ratio would mask real drift for up
            # to `window_size` future observations, not just one.
            raise DriftMonitorError(f"score must be a finite number (no NaN/Infinity), got {score}")
        self._recent.append(score)
        if len(self._recent) < self._window_size:
            return None
        recent_mean = sum(self._recent) / len(self._recent)
        ratio = recent_mean / self._baseline_mean
        return DriftReport(
            baseline_mean_score=self._baseline_mean,
            recent_mean_score=recent_mean,
            drift_ratio=ratio,
            drifted=ratio >= self._drift_ratio_threshold,
        )
