# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - tests/test_realistic_false_positives.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""How often a healthy machine is called anomalous when its speed wanders, its load changes and its noise is pink.

The measured picture (kept here so it is not rediscovered): with a few hundred training windows the worst healthy score stays near 6
and the default threshold of 10 never fires; with only 20 training windows and a machine that varies a lot, the per-bin standard
deviation is badly estimated and 1 to 12 healthy windows in a hundred are called anomalous, with or without a Hann window. A fault of
the size used here scores above 100 in every case, so the detector separates them; what is missing is a threshold that follows the
variation of the machine (the design change that is pending a decision). The last test states the aim and is expected to fail until then.
"""
from __future__ import annotations

import numpy as np
import pytest

from hydra_umc_anomaly_detector.detector import AnomalyDetector
from realistic_signals import Machine

RATE = 1000.0


def _scores(machine: Machine, detector: AnomalyDetector, count: int) -> np.ndarray:
    return np.array([detector.score(machine.healthy()).score for _ in range(count)])


@pytest.mark.parametrize("window", ["none", "hann"])
def test_with_enough_training_windows_a_wandering_machine_raises_no_false_alarm(window):
    machine = Machine(11, speed_jitter=0.03, load_variation=0.3, noise=0.2)
    detector = AnomalyDetector(RATE, threshold=10.0, window=window)
    detector.fit(machine.healthy_windows(200))
    scores = _scores(machine, detector, 1500)
    assert (scores > detector.threshold).mean() < 0.001
    assert scores.max() < detector.threshold * 0.8  # and not by a hair


@pytest.mark.parametrize("window", ["none", "hann"])
def test_a_real_fault_stands_far_above_the_healthy_scores(window):
    machine = Machine(12, speed_jitter=0.03, load_variation=0.3, noise=0.2)
    detector = AnomalyDetector(RATE, threshold=10.0, window=window)
    detector.fit(machine.healthy_windows(200))
    healthy = _scores(machine, detector, 500)
    faults = np.array([detector.score(machine.faulty()).score for _ in range(100)])
    assert faults.min() > 5 * healthy.max()
    assert all(detector.score(machine.faulty()).anomalous for _ in range(20))


def test_few_training_windows_on_a_varying_machine_do_raise_false_alarms_today():
    """The limitation, stated as a measurement: not a goal."""
    machine = Machine(13, speed_jitter=0.05, load_variation=0.4, noise=0.3)
    detector = AnomalyDetector(RATE, threshold=10.0)
    detector.fit(machine.healthy_windows(20))
    assert (_scores(machine, detector, 1500) > detector.threshold).mean() > 0.01


@pytest.mark.xfail(reason="the threshold does not follow the variation of the machine yet (statistical redesign pending a decision)", strict=False)
def test_the_aim_even_20_training_windows_keep_false_alarms_under_a_tenth_of_a_percent():
    machine = Machine(13, speed_jitter=0.05, load_variation=0.4, noise=0.3)
    detector = AnomalyDetector(RATE, threshold=10.0)
    detector.fit(machine.healthy_windows(20))
    assert (_scores(machine, detector, 1500) > detector.threshold).mean() < 0.001
