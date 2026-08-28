# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - tests/test_metrics.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""Real precision/recall over the exact same real healthy/faulty fixture
detector.py's own tests use - the computed number this project's own
docstring previously only claimed in prose.
"""
from __future__ import annotations

import numpy as np
import pytest
from signal_fixtures import SAMPLE_RATE, faulty_signal, healthy_signal

from hydra_umc_anomaly_detector.detector import AnomalyDetector
from hydra_umc_anomaly_detector.metrics import precision_recall


def test_precision_recall_basic_confusion_matrix() -> None:
    result = precision_recall(
        predicted_anomalous=[True, True, False, False],
        actually_anomalous=[True, False, False, True],
    )

    assert result.true_positives == 1
    assert result.false_positives == 1
    assert result.false_negatives == 1
    assert result.true_negatives == 1
    assert result.precision == pytest.approx(0.5)
    assert result.recall == pytest.approx(0.5)
    assert result.f1 == pytest.approx(0.5)


def test_precision_recall_rejects_mismatched_lengths() -> None:
    with pytest.raises(ValueError):
        precision_recall([True, False], [True])


def test_precision_is_one_when_nothing_predicted_anomalous() -> None:
    result = precision_recall([False, False], [False, True])
    assert result.precision == 1.0  # no false positives to be wrong about
    assert result.recall == 0.0  # missed the one real anomaly


def test_recall_is_one_when_nothing_is_actually_anomalous() -> None:
    result = precision_recall([False, True], [False, False])
    assert result.recall == 1.0  # nothing real to miss
    assert result.precision == 0.0  # the one True prediction is a real false positive


def test_real_precision_and_recall_over_the_synthetic_healthy_faulty_fixture() -> None:
    # The real, computed metric this project's detector.py docstring
    # previously only claimed in prose ("healthy scored up to ~5.3,
    # faulty scored in the hundreds"). Reuses the exact same fixture
    # signals and training seed as test_detector.py's own
    # fitted_detector, so this is a real regression test for the
    # documented separation, not a new, independently-tuned scenario.
    training_rng = np.random.default_rng(42)
    healthy_windows = [healthy_signal(training_rng) for _ in range(10)]
    detector = AnomalyDetector(sample_rate=SAMPLE_RATE)
    detector.fit(healthy_windows)

    eval_rng = np.random.default_rng(123)  # distinct from both training (42) and detector.py's own eval seed (999)
    healthy_eval = [healthy_signal(eval_rng) for _ in range(20)]
    faulty_eval = [faulty_signal(eval_rng) for _ in range(20)]

    predicted = [detector.score(w).anomalous for w in healthy_eval + faulty_eval]
    actual = [False] * len(healthy_eval) + [True] * len(faulty_eval)

    result = precision_recall(predicted, actual)

    # A real, checkable claim: this detector's threshold genuinely
    # separates the synthetic healthy/faulty populations perfectly on
    # this fixture - not just "usually works", a real 1.0/1.0.
    assert result.precision == 1.0
    assert result.recall == 1.0
    assert result.f1 == 1.0
