# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - src/hydra_umc_anomaly_detector/spectrogram.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""A short-time spectrogram and a finder of transients in it.

`fft.py` gives the spectrum of one whole window, so a knock that lasts a few milliseconds inside a long window is averaged away. The
spectrogram cuts the window into overlapping frames (Hann taper, one FFT per frame) so the energy can be followed in time and a
burst shows as a few frames that stand far above the rest. The finder is robust on purpose: the level of a frame is compared with
the median of all frames in units of the median absolute deviation (MAD times 1.4826), so the burst itself does not inflate the
scale it is judged by.

Everything is computed on recorded or simulated signals (evidence: simulator); nothing here has been checked on a real machine.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

MIN_FRAME = 8
DEFAULT_TRANSIENT_THRESHOLD = 6.0
_MAD_TO_SIGMA = 1.4826


@dataclass(frozen=True)
class Spectrogram:
    """`magnitudes[i, j]` is the amplitude at `freqs[j]` Hz in the frame that starts at `times[i]` seconds (DC bin dropped)."""

    times: np.ndarray
    freqs: np.ndarray
    magnitudes: np.ndarray

    def frame_energy(self) -> np.ndarray:
        """The energy of each frame (sum of the squared magnitudes), one value per frame."""
        return np.sum(self.magnitudes ** 2, axis=1)

    def band_energy(self, low_hz: float, high_hz: float) -> np.ndarray:
        """The energy of each frame between `low_hz` (included) and `high_hz` (excluded)."""
        if high_hz <= low_hz:
            raise ValueError("high_hz must be above low_hz")
        mask = (self.freqs >= low_hz) & (self.freqs < high_hz)
        return np.sum(self.magnitudes[:, mask] ** 2, axis=1)


def compute_spectrogram(signal: np.ndarray | list[float], sample_rate: float, frame: int = 256, hop: int | None = None) -> Spectrogram:
    """Cuts `signal` into frames of `frame` samples every `hop` samples (half a frame by default) and takes the Hann-windowed
    amplitude spectrum of each. A signal shorter than one frame is refused, never padded."""
    data = np.asarray(signal, dtype=np.float64)
    if data.ndim != 1:
        raise ValueError("signal must be 1-D")
    if sample_rate <= 0 or not np.isfinite(sample_rate):
        raise ValueError("sample_rate must be positive")
    if frame < MIN_FRAME:
        raise ValueError(f"frame must be at least {MIN_FRAME} samples")
    step = frame // 2 if hop is None else hop
    if step < 1:
        raise ValueError("hop must be at least 1 sample")
    if len(data) < frame:
        raise ValueError(f"signal has {len(data)} samples, fewer than one frame ({frame})")
    if not np.isfinite(data).all():
        raise ValueError("signal must contain only finite numbers (no NaN/Infinity)")
    taper = 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(frame) / frame)
    starts = list(range(0, len(data) - frame + 1, step))
    rows = []
    for start in starts:
        part = data[start:start + frame]
        rows.append(np.abs(np.fft.rfft((part - part.mean()) * taper)) * (2.0 / taper.sum()))
    freqs = np.fft.rfftfreq(frame, d=1.0 / sample_rate)
    return Spectrogram(times=np.array(starts, dtype=np.float64) / sample_rate, freqs=freqs[1:], magnitudes=np.array(rows)[:, 1:])


def find_transients(energy: np.ndarray | list[float], threshold: float = DEFAULT_TRANSIENT_THRESHOLD) -> list[int]:
    """Indexes of the frames whose energy is more than `threshold` robust deviations above the median of all frames.

    The deviation is the MAD times 1.4826 of the logarithm of the energy (a burst is ten times, not ten units, above the floor). When
    more than half of the frames carry the same energy the MAD is zero and no frame can be judged, so the answer is an empty list
    rather than a flood of false alarms."""
    values = np.asarray(energy, dtype=np.float64)
    if values.ndim != 1:
        raise ValueError("energy must be 1-D")
    if threshold <= 0:
        raise ValueError("threshold must be positive")
    if len(values) < 3 or not np.isfinite(values).all() or (values < 0).any():
        return []
    logs = np.log10(values + 1e-12)
    centre = float(np.median(logs))
    spread = float(np.median(np.abs(logs - centre))) * _MAD_TO_SIGMA
    if spread <= 0.0:
        return []
    return [int(i) for i in np.nonzero((logs - centre) / spread > threshold)[0]]
