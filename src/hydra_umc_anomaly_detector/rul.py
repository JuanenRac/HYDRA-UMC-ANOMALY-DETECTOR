# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - src/hydra_umc_anomaly_detector/rul.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Remaining useful life, in the simplest honest form: how many days until a score reaches a limit.

The trend of the drift score (or of any score that rises as a machine wears) is fitted with the
Theil-Sen method: the slope is the median of the slopes between every two points, so a few wild
points do not move it the way they move a least-squares line. The days left are the time the
fitted line needs to go from where it is now to the limit. The answer is an estimate of a trend,
not a promise: it says so with a range (the 25th and 75th percentile of the pairwise slopes) and
it says "not enough data" or "not rising" instead of inventing a number.

Nothing here stops a machine or opens a work order; it only reports.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

SECONDS_PER_DAY = 86400.0
MAX_POINTS = 1000


class RulError(ValueError):
    """The points or the limit cannot be used."""


@dataclass(frozen=True)
class RulEstimate:
    status: str  # insufficient_data | not_rising | limit_reached | estimated
    points: int
    slope_per_day: float | None
    current_value: float | None
    days_to_limit: float | None
    days_low: float | None  # the steepest plausible trend: the earliest the limit may be reached
    days_high: float | None  # the gentlest plausible trend: the latest, None when that trend does not rise

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "points": self.points,
            "slopePerDay": self.slope_per_day,
            "currentValue": self.current_value,
            "daysToLimit": self.days_to_limit,
            "daysLow": self.days_low,
            "daysHigh": self.days_high,
        }


def _days(limit: float, now: float, slope_per_s: float) -> float | None:
    if now >= limit:
        return 0.0
    if slope_per_s <= 0:
        return None
    return (limit - now) / slope_per_s / SECONDS_PER_DAY


def estimate_rul(times_s: list[float], scores: list[float], limit: float, *, min_points: int = 8) -> RulEstimate:
    """Days until the trend of `scores` (taken at `times_s`, in seconds) reaches `limit`."""
    if len(times_s) != len(scores):
        raise RulError("times and scores must have the same length")
    if len(times_s) > MAX_POINTS:
        raise RulError(f"at most {MAX_POINTS} points are accepted, got {len(times_s)}")
    if isinstance(min_points, bool) or not isinstance(min_points, int) or min_points < 3:
        raise RulError("min_points must be an integer of at least 3")
    if isinstance(limit, bool) or not isinstance(limit, (int, float)) or not math.isfinite(limit):
        raise RulError("limit must be a finite number")
    for label, values in (("times", times_s), ("scores", scores)):
        for value in values:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise RulError(f"{label} must all be finite numbers")
    n = len(times_s)
    if n < min_points:
        return RulEstimate("insufficient_data", n, None, None, None, None, None)
    order = np.argsort(np.asarray(times_s, dtype=float), kind="stable")
    t = np.asarray(times_s, dtype=float)[order]
    y = np.asarray(scores, dtype=float)[order]
    if t[-1] == t[0]:
        return RulEstimate("insufficient_data", n, None, None, None, None, None)
    i, j = np.triu_indices(n, k=1)
    dt = t[j] - t[i]
    keep = dt > 0
    slopes = (y[j][keep] - y[i][keep]) / dt[keep]
    slope = float(np.median(slopes))
    low_slope, high_slope = (float(v) for v in np.percentile(slopes, [75, 25]))
    intercept = float(np.median(y - slope * t))
    now = slope * float(t[-1]) + intercept
    if now >= limit:
        return RulEstimate("limit_reached", n, slope * SECONDS_PER_DAY, now, 0.0, 0.0, 0.0)
    if slope <= 0:
        return RulEstimate("not_rising", n, slope * SECONDS_PER_DAY, now, None, _days(limit, now, low_slope), None)
    return RulEstimate(
        "estimated", n, slope * SECONDS_PER_DAY, now,
        _days(limit, now, slope), _days(limit, now, low_slope), _days(limit, now, high_slope),
    )
