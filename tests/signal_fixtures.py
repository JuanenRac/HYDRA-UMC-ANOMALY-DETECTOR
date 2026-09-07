# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - tests/signal_fixtures.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""Shared real synthetic signal generators - test-only, not a test file
itself (no test_ prefix, so pytest never collects it directly). Used by
test_detector.py, test_metrics.py and test_drift.py so the same real
"healthy motor" / "faulty motor" fixtures back every real claim this
project makes about its own detection quality, instead of each test
file inventing a slightly different signal.
"""
from __future__ import annotations

import numpy as np

SAMPLE_RATE = 1000.0
N_SAMPLES = 500


def healthy_signal(rng: np.random.Generator) -> np.ndarray:
    """A 50 Hz fundamental (e.g. a motor's rotation rate) plus small
    random measurement noise - a real, if synthetic, stand-in for a
    healthy motor's vibration signature."""
    t = np.arange(N_SAMPLES) / SAMPLE_RATE
    return np.sin(2 * np.pi * 50.0 * t) + 0.05 * rng.standard_normal(N_SAMPLES)


def faulty_signal(rng: np.random.Generator, *, fault_amplitude: float = 0.8) -> np.ndarray:
    """The same healthy signal PLUS a new component at 137 Hz - standing
    in for a real bearing-defect frequency that would not be present in
    a healthy unit. `fault_amplitude` is exposed (default matches the
    original fixed-strength fault) so drift simulation can ramp it up
    gradually instead of only ever testing the fully-developed fault."""
    t = np.arange(N_SAMPLES) / SAMPLE_RATE
    fault_component = fault_amplitude * np.sin(2 * np.pi * 137.0 * t)
    return healthy_signal(rng) + fault_component
