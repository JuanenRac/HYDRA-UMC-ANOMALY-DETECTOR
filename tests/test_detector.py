# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - tests/test_detector.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""The tests that matter most for this project's actual promise: fit on
synthetic "healthy motor" signals, then confirm a real synthetic fault
(an extra strong harmonic - a textbook stand-in for a bearing defect or
loose-belt frequency) is flagged, while a genuinely healthy-looking
reading is not. A fixed numpy random seed keeps this deterministic
instead of an occasionally-flaky statistical test.
"""
from __future__ import annotations

import numpy as np
import pytest

from hydra_umc_anomaly_detector.detector import AnomalyDetector, NotFittedError

SAMPLE_RATE = 1000.0
N_SAMPLES = 500


def _healthy_signal(rng: np.random.Generator) -> np.ndarray:
    """A 50 Hz fundamental (e.g. a motor's rotation rate) plus small
    random measurement noise - a real, if synthetic, stand-in for a
    healthy motor's vibration signature."""
    t = np.arange(N_SAMPLES) / SAMPLE_RATE
    return np.sin(2 * np.pi * 50.0 * t) + 0.05 * rng.standard_normal(N_SAMPLES)


def _faulty_signal(rng: np.random.Generator) -> np.ndarray:
    """The same healthy signal PLUS a strong new component at 137 Hz -
    standing in for a real bearing-defect frequency that would not be
    present in a healthy unit."""
    t = np.arange(N_SAMPLES) / SAMPLE_RATE
    fault_component = 0.8 * np.sin(2 * np.pi * 137.0 * t)
    return _healthy_signal(rng) + fault_component


@pytest.fixture()
def fitted_detector() -> AnomalyDetector:
    rng = np.random.default_rng(42)
    healthy_windows = [_healthy_signal(rng) for _ in range(10)]
    detector = AnomalyDetector(sample_rate=SAMPLE_RATE)
    detector.fit(healthy_windows)
    return detector


def test_score_raises_before_fit() -> None:
    detector = AnomalyDetector(sample_rate=SAMPLE_RATE)
    assert not detector.is_fitted
    with pytest.raises(NotFittedError):
        detector.score([0.0] * N_SAMPLES)


def test_healthy_looking_signal_is_not_flagged(fitted_detector: AnomalyDetector) -> None:
    rng = np.random.default_rng(999)  # different seed than training - a genuinely new sample
    verdict = fitted_detector.score(_healthy_signal(rng))
    assert not verdict.anomalous, f"a healthy-looking signal scored {verdict.score:.1f} (threshold exceeded)"


def test_faulty_signal_with_extra_harmonic_is_flagged(fitted_detector: AnomalyDetector) -> None:
    rng = np.random.default_rng(999)
    verdict = fitted_detector.score(_faulty_signal(rng))
    assert verdict.anomalous, f"a synthetically faulty signal only scored {verdict.score:.1f}"
    # The detector should point at (roughly) the actual fault frequency,
    # not just flag "something's wrong" - real diagnostic value.
    assert verdict.worst_bin_freq == pytest.approx(137.0, abs=5.0)


def test_threshold_is_configurable() -> None:
    rng = np.random.default_rng(42)
    healthy_windows = [_healthy_signal(rng) for _ in range(10)]

    lenient = AnomalyDetector(sample_rate=SAMPLE_RATE, threshold=1000.0)
    lenient.fit(healthy_windows)
    verdict = lenient.score(_faulty_signal(np.random.default_rng(999)))
    assert not verdict.anomalous, "an absurdly high threshold must not flag anything"
