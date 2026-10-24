# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - tests/realistic_signals.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Healthy signals that vary the way a real machine does: the speed wanders, the load changes the amplitude, the phase is
whatever it was when the window started, a second harmonic rides along and the noise is pink (1/f), not white. The fixtures of the
detector tests use one frequency on a bin, one amplitude and one phase, which is the easiest case there is."""
from __future__ import annotations

import numpy as np


class Machine:
    def __init__(self, seed: int, *, sample_rate: float = 1000.0, window: int = 512, base_hz: float = 50.0,
                 speed_jitter: float = 0.01, load_variation: float = 0.2, noise: float = 0.1) -> None:
        self.rng = np.random.default_rng(seed)
        self.sample_rate = sample_rate
        self.window = window
        self.base_hz = base_hz
        self.speed_jitter = speed_jitter
        self.load_variation = load_variation
        self.noise = noise

    def _pink(self, n: int) -> np.ndarray:
        spectrum = np.fft.rfft(self.rng.standard_normal(n))
        frequencies = np.fft.rfftfreq(n)
        frequencies[0] = 1.0
        shaped = np.fft.irfft(spectrum / np.sqrt(frequencies), n)
        return shaped / shaped.std()

    def healthy(self) -> np.ndarray:
        speed = self.base_hz * (1 + self.rng.uniform(-self.speed_jitter, self.speed_jitter))
        amplitude = 1 + self.rng.uniform(-self.load_variation, self.load_variation)
        phase = self.rng.uniform(0, 2 * np.pi)
        t = np.arange(self.window) / self.sample_rate
        return (
            amplitude * np.sin(2 * np.pi * speed * t + phase)
            + 0.3 * amplitude * np.sin(2 * np.pi * 2 * speed * t + 2 * phase)
            + self.noise * self._pink(self.window)
        )

    def faulty(self, fault_hz: float = 173.0, fault_amplitude: float = 0.8) -> np.ndarray:
        t = np.arange(self.window) / self.sample_rate
        return self.healthy() + fault_amplitude * np.sin(2 * np.pi * fault_hz * t)

    def healthy_windows(self, count: int) -> list[np.ndarray]:
        return [self.healthy() for _ in range(count)]
