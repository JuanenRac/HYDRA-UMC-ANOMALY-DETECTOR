# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - src/hydra_umc_anomaly_detector/orders.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Order tracking: a signal re-sampled by the angle of the shaft instead of by time.

A fault of a rotating machine repeats once per turn (or a fixed number of times per turn), so its
frequency follows the speed. In a window where the speed changes, that peak is smeared across many
bins and a baseline made at another speed calls it new. Re-sampled by angle (a fixed number of
samples per revolution), the peak stays on the same *order* (multiples of the shaft speed) whatever
the speed is, and the FFT of the re-sampled signal has orders, not hertz, on its axis.

The speed has to be known for every sample (a tachometer, an encoder or a speed read from the drive);
this module does not estimate it from the signal. The re-sampling is a linear interpolation, with no
low-pass filter: choose `samples_per_rev` at least twice the highest order of interest and below the
samples per revolution the original rate gives at the highest speed (`max_clean_order`), otherwise
the orders above that fold back. Checked with synthetic signals only, never on a real machine.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from .fft import Spectrum, compute_spectrum

MAX_REVOLUTIONS = 4096
MIN_REVOLUTIONS = 2


class OrderError(ValueError):
    """The signal, the speed or the settings cannot be used for order tracking."""


@dataclass(frozen=True)
class OrderSettings:
    """`enabled` is off by default; the rest only matters once it is on."""

    enabled: bool = False
    samples_per_rev: int = 64
    # 0 means "every complete revolution of the window"; a fixed number gives windows of the same length (needed to fit a baseline)
    revolutions: int = 0
    min_rpm: float = 30.0


def parse_order_settings(raw: object) -> OrderSettings:
    if raw is None:
        return OrderSettings()
    if not isinstance(raw, dict):
        raise OrderError("orderTracking must be an object")
    known = {"enabled": "enabled", "samplesPerRev": "samples_per_rev", "revolutions": "revolutions", "minRpm": "min_rpm"}
    unknown = sorted(set(raw) - set(known))
    if unknown:
        raise OrderError("orderTracking has unknown keys: " + ", ".join(unknown))
    values = {known[k]: v for k, v in raw.items()}
    enabled = values.get("enabled", False)
    if not isinstance(enabled, bool):
        raise OrderError("orderTracking.enabled must be true or false")
    spr = values.get("samples_per_rev", 64)
    revs = values.get("revolutions", 0)
    if isinstance(spr, bool) or not isinstance(spr, int) or not 8 <= spr <= 4096:
        raise OrderError("orderTracking.samplesPerRev must be an integer from 8 to 4096")
    if isinstance(revs, bool) or not isinstance(revs, int) or (revs != 0 and not MIN_REVOLUTIONS <= revs <= MAX_REVOLUTIONS):
        raise OrderError(f"orderTracking.revolutions must be 0 or an integer from {MIN_REVOLUTIONS} to {MAX_REVOLUTIONS}")
    min_rpm = values.get("min_rpm", 30.0)
    if isinstance(min_rpm, bool) or not isinstance(min_rpm, (int, float)) or not math.isfinite(min_rpm) or min_rpm <= 0:
        raise OrderError("orderTracking.minRpm must be a positive number")
    return OrderSettings(enabled=enabled, samples_per_rev=spr, revolutions=revs, min_rpm=float(min_rpm))


def max_clean_order(sample_rate: float, max_rpm: float) -> float:
    """The highest order that the original sampling still holds at the top speed (its Nyquist order)."""
    if sample_rate <= 0 or max_rpm <= 0:
        raise OrderError("sample_rate and max_rpm must be positive")
    return sample_rate * 60.0 / max_rpm / 2.0


def _speed_array(rpm: float | Sequence[float] | np.ndarray, count: int, min_rpm: float) -> np.ndarray:
    if isinstance(rpm, (int, float)) and not isinstance(rpm, bool):
        speed = np.full(count, float(rpm))
    else:
        try:
            speed = np.asarray(rpm, dtype=np.float64)
        except (TypeError, ValueError) as error:
            raise OrderError("rpm must be a number or a list of numbers") from error
        if speed.ndim != 1 or len(speed) != count:
            raise OrderError(f"rpm must be one number or one number per sample ({count})")
    if not np.isfinite(speed).all():
        raise OrderError("rpm must contain only finite numbers")
    if float(speed.min()) < min_rpm:
        raise OrderError(f"rpm must be at least {min_rpm:g} for the whole window (order tracking cannot follow a stopped shaft)")
    return speed


def resample_by_angle(
    signal: Sequence[float] | np.ndarray,
    sample_rate: float,
    rpm: float | Sequence[float] | np.ndarray,
    samples_per_rev: int = 64,
    revolutions: int = 0,
    min_rpm: float = 30.0,
) -> np.ndarray:
    """The signal at `samples_per_rev` equally spaced angles per revolution.

    `rpm` is one number (constant speed) or one number per sample. With `revolutions` 0 the result holds every complete
    revolution of the window; with a number it holds exactly that many (an error when the window is shorter), so every
    window has the same length."""
    data = np.asarray(signal, dtype=np.float64)
    if data.ndim != 1 or len(data) < 2:
        raise OrderError("signal must be a 1-D array of at least 2 samples")
    if not np.isfinite(data).all():
        raise OrderError("signal must contain only finite numbers (no NaN/Infinity)")
    if not (math.isfinite(sample_rate) and sample_rate > 0):
        raise OrderError("sample_rate must be positive")
    if samples_per_rev < 8:
        raise OrderError("samples_per_rev must be at least 8")
    speed = _speed_array(rpm, len(data), min_rpm)
    # the angle (in revolutions) at every sample: the integral of the speed, trapezoid rule; strictly increasing because rpm > 0
    phase = np.concatenate(([0.0], np.cumsum((speed[:-1] + speed[1:]) / 2.0 / 60.0 / sample_rate)))
    total = float(phase[-1])
    available = int(math.floor(total))
    if revolutions:
        if available < revolutions:
            raise OrderError(f"the window holds {total:.2f} revolutions, fewer than the {revolutions} asked for")
        wanted = revolutions
    else:
        wanted = min(available, MAX_REVOLUTIONS)
        if wanted < MIN_REVOLUTIONS:
            raise OrderError(f"the window holds {total:.2f} revolutions; at least {MIN_REVOLUTIONS} are needed")
    grid = np.arange(wanted * samples_per_rev, dtype=np.float64) / samples_per_rev
    return np.asarray(np.interp(grid, phase, data))


def order_spectrum(
    signal: Sequence[float] | np.ndarray,
    sample_rate: float,
    rpm: float | Sequence[float] | np.ndarray,
    settings: OrderSettings | None = None,
    window: str = "hann",
) -> Spectrum:
    """The amplitude spectrum of the angle-resampled signal; `freqs` are orders (1.0 = the shaft speed)."""
    cfg = settings if settings is not None else OrderSettings()
    resampled = resample_by_angle(signal, sample_rate, rpm, cfg.samples_per_rev, cfg.revolutions, cfg.min_rpm)
    return compute_spectrum(resampled, float(cfg.samples_per_rev), window)
