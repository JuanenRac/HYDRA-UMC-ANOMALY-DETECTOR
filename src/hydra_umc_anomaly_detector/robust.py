# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - src/hydra_umc_anomaly_detector/robust.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The robust scoring: the same idea as the z-score of the baseline (how far is this window from the healthy ones) with the weak points of that z-score removed.

The original score is the worst per-bin z-score of the raw magnitudes, against a threshold that somebody has to guess. These things make it fragile and each has an
answer here, all of them off until `scoring: "robust"` is asked for (the original behaviour is still the default):

* **The level of the signal.** A motor under a different load vibrates harder at every frequency, and every bin moves together. Here the magnitudes are divided by the
  RMS of the window (after the mean is removed) before anything else, so only the shape of the spectrum is compared, and then the logarithm (base 10) is taken, so a
  peak 10 times higher is one unit more at any height of the spectrum, not 10 times more at the top.
* **The mean and the standard deviation.** One odd healthy window moves the mean and inflates the standard deviation of every bin, and the next fault hides in it. The
  centre of a bin is its median. Its spread is the larger of the median absolute deviation (MAD) times 1.4826, which equals the standard deviation of a normal
  distribution and ignores the odd one out, and the distance between the 10th and the 90th percentile divided by 2.5631, which sees a peak that wanders in and out of a
  bin (a speed that varies) and that the MAD alone would call "never there".
* **A null is not a fault.** In a healthy window the magnitude of a noise bin can fall to almost nothing, and in the logarithm that is a large negative deviation. By
  default (`side: "up"`) only a bin that is higher than usual counts, which is what a new peak at a bearing frequency is; `"both"` counts the size of the deviation in
  either direction.
* **One bin against two hundred and fifty.** The maximum over many bins grows with their number. With `bands` above 0 the bins are cut into that many contiguous bands
  and the score of a window is the highest band score, a band scoring the root of the mean of the squares of its z-scores: a fault that shows in a few neighbouring bins
  stands out and a single noisy bin does not.
* **The threshold nobody can guess.** The healthy windows are split (every third one is kept apart, `validationFraction`), the baseline is made with the rest, the held
  out windows are scored with it and the threshold is the quantile `quantile` (0.99 by default) of those scores times `margin` (1.5 by default). The threshold, the
  settings and the number of windows go into the saved baseline, so a baseline file says by itself how it must be used.

Windows can be Hann or Welch as before (the spectrum is computed by `fft.py`). Nothing here proves that the false alarm rate is what the quantile suggests: with a few
tens of windows the quantile is one of the largest scores seen, and a window of a machine in a state the healthy recordings did not cover will still be a false alarm.
Recalibrate with real healthy recordings before an alarm is trusted.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .fft import compute_spectrum

MAD_TO_SIGMA = 1.4826
# the distance between the 10th and the 90th percentile of a normal distribution, in standard deviations
QUANTILE_RANGE_TO_SIGMA = 2.5631
MIN_VALIDATION = 3
MIN_TRAINING = 8


class RobustError(ValueError):
    """The robust settings or the windows they are applied to cannot be used."""


@dataclass(frozen=True)
class RobustSettings:
    quantile: float = 0.99
    margin: float = 1.5
    validation_fraction: float = 0.3
    bands: int = 0
    side: str = "up"
    normalize: bool = True
    min_windows: int = 20
    # floors, so that a silent window or a bin that is the same in every healthy window never divides by zero
    energy_floor: float = 1e-12
    min_scale: float = 0.02

    def __post_init__(self) -> None:
        def number(value: object, name: str, low: float, high: float) -> None:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise RobustError(f"{name} must be a number from {low:g} to {high:g}")

        number(self.quantile, "quantile", 0.5, 1.0)
        number(self.margin, "margin", 1.0, 100.0)
        number(self.validation_fraction, "validationFraction", 0.1, 0.5)
        number(self.energy_floor, "energyFloor", 1e-300, 1.0)
        number(self.min_scale, "minScale", 1e-9, 10.0)
        if isinstance(self.bands, bool) or not isinstance(self.bands, int) or not (self.bands == 0 or 2 <= self.bands <= 256):
            raise RobustError("bands must be 0 (one score per bin) or an integer from 2 to 256")
        if isinstance(self.min_windows, bool) or not isinstance(self.min_windows, int) or self.min_windows < MIN_TRAINING + MIN_VALIDATION:
            raise RobustError(f"minWindows must be an integer of at least {MIN_TRAINING + MIN_VALIDATION}")
        if self.side not in ("up", "both"):
            raise RobustError('side must be "up" or "both"')
        if not isinstance(self.normalize, bool):
            raise RobustError("normalize must be true or false")

    def describe(self) -> dict[str, object]:
        return {"quantile": self.quantile, "margin": self.margin, "validationFraction": self.validation_fraction, "bands": self.bands, "side": self.side,
                "normalize": self.normalize, "minWindows": self.min_windows, "energyFloor": self.energy_floor, "minScale": self.min_scale}


_KEYS = {"quantile": "quantile", "margin": "margin", "validationFraction": "validation_fraction", "bands": "bands", "side": "side", "normalize": "normalize",
         "minWindows": "min_windows", "energyFloor": "energy_floor", "minScale": "min_scale"}


def parse_robust_settings(raw: object) -> RobustSettings:
    """The `robust` object of the detector configuration file."""
    if not isinstance(raw, dict) or set(raw) - set(_KEYS):
        raise RobustError(f"robust is an object with the keys {sorted(_KEYS)}")
    return RobustSettings(**{_KEYS[k]: v for k, v in raw.items()})


def features(signal: np.ndarray | list[float], sample_rate: float, window: str, settings: RobustSettings) -> tuple[np.ndarray, np.ndarray]:
    """(freqs, features): the log10 magnitude of every bin, of the spectrum divided by the RMS of the window when `normalize` is set."""
    data = np.asarray(signal, dtype=np.float64)
    spectrum = compute_spectrum(data, sample_rate, window)
    magnitudes = spectrum.magnitudes
    if settings.normalize:
        rms = float(np.sqrt(np.mean((data - data.mean()) ** 2)))
        magnitudes = magnitudes / max(rms, settings.energy_floor)
    return spectrum.freqs, np.log10(np.maximum(magnitudes, settings.energy_floor))


def score_features(z: np.ndarray, bands: int, side: str = "up") -> tuple[float, int]:
    """(score, index of the bin that drives it): the worst z over the bins, or the highest band score (the root of the mean of the squares in the band).
    With `side="up"` only a bin that is higher than usual counts (a new peak is a fault; a deep null of the noise in a healthy window is not), with `"both"` the size of
    the deviation counts in either direction."""
    magnitude = np.abs(z) if side == "both" else np.maximum(z, 0.0)
    if bands <= 0:
        index = int(np.argmax(magnitude))
        return float(magnitude[index]), index
    best, best_index = -1.0, 0
    offset = 0
    for part in np.array_split(magnitude, min(bands, len(magnitude))):
        value = float(np.sqrt(np.mean(part ** 2)))
        if value > best:
            best, best_index = value, offset + int(np.argmax(part))
        offset += len(part)
    return best, best_index


def split_windows(count: int, fraction: float) -> tuple[list[int], list[int]]:
    """(training indexes, validation indexes): every `round(1/fraction)`-th window is held out, so the split depends on the order and on nothing else."""
    stride = max(2, round(1.0 / fraction))
    validation = [i for i in range(count) if i % stride == stride - 1]
    training = [i for i in range(count) if i % stride != stride - 1]
    return training, validation
