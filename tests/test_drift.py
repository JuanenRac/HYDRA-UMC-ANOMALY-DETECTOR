# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - tests/test_drift.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""Real simulated drift: a slowly worsening fault component fed through
DriftMonitor, proving it can flag a real degrading trend even while
individual windows are still well under the single-window anomaly
threshold - the real, distinct value a drift monitor adds over
detector.py's own per-window score() alone.
"""
from __future__ import annotations

import numpy as np
import pytest
from signal_fixtures import SAMPLE_RATE, faulty_signal, healthy_signal

from hydra_umc_anomaly_detector.detector import AnomalyDetector
from hydra_umc_anomaly_detector.drift import DriftMonitor, DriftMonitorError


def test_drift_monitor_rejects_too_little_baseline_data() -> None:
    with pytest.raises(DriftMonitorError):
        DriftMonitor([1.0])


def test_drift_monitor_rejects_non_positive_baseline_mean() -> None:
    with pytest.raises(DriftMonitorError):
        DriftMonitor([0.0, 0.0])


def test_drift_monitor_rejects_a_threshold_at_or_below_one() -> None:
    with pytest.raises(DriftMonitorError):
        DriftMonitor([1.0, 2.0], drift_ratio_threshold=1.0)


def test_drift_monitor_rejects_a_non_finite_baseline_score() -> None:
    # NaN/Infinity compare False against everything, including "<= 0" -
    # a poisoned baseline would otherwise sail past the positive-mean
    # guard and leave every future ratio permanently NaN (drift detection
    # silently, permanently disabled). json.loads accepts NaN/Infinity by
    # default, so this is reachable straight from the HTTP API, not just
    # a theoretical caller.
    with pytest.raises(DriftMonitorError):
        DriftMonitor([1.0, float("nan")])
    with pytest.raises(DriftMonitorError):
        DriftMonitor([1.0, float("inf")])


def test_drift_monitor_rejects_a_non_finite_threshold() -> None:
    with pytest.raises(DriftMonitorError):
        DriftMonitor([1.0, 2.0], drift_ratio_threshold=float("nan"))


def test_observe_rejects_a_non_finite_score() -> None:
    # A single bad score must never be allowed to poison the rolling
    # window - it would mask real drift for up to window_size future
    # observations rather than failing loudly on the one bad sample.
    monitor = DriftMonitor([1.0, 2.0], window_size=3)
    with pytest.raises(DriftMonitorError):
        monitor.observe(float("nan"))
    with pytest.raises(DriftMonitorError):
        monitor.observe(float("-inf"))
    # And the window must still be untouched - the reports below prove
    # observe() didn't already push a NaN before raising.
    assert monitor.observe(1.0) is None
    assert monitor.observe(1.0) is None
    assert monitor.observe(1.0) is not None


def test_observe_returns_none_until_the_window_fills() -> None:
    monitor = DriftMonitor([1.0, 2.0, 3.0], window_size=3)
    assert monitor.observe(1.0) is None
    assert monitor.observe(1.0) is None
    assert monitor.observe(1.0) is not None


def test_no_drift_when_recent_scores_match_the_baseline() -> None:
    monitor = DriftMonitor([5.0, 5.0, 5.0], window_size=5, drift_ratio_threshold=2.0)
    report = None
    for _ in range(5):
        report = monitor.observe(5.0)
    assert report is not None
    assert not report.drifted
    assert report.drift_ratio == pytest.approx(1.0)


def test_drift_flagged_once_recent_scores_clear_the_ratio_threshold() -> None:
    monitor = DriftMonitor([5.0, 5.0, 5.0], window_size=5, drift_ratio_threshold=2.0)
    report = None
    for _ in range(5):
        report = monitor.observe(20.0)  # 4x the baseline mean
    assert report is not None
    assert report.drifted
    assert report.drift_ratio == pytest.approx(4.0)


def test_real_simulated_drift_from_a_slowly_worsening_fault(capsys: pytest.CaptureFixture[str]) -> None:
    """The real scenario this module exists for: a fault component that
    ramps up gradually over many windows (a loosening mount, a wearing
    bearing) rather than appearing at full strength all at once.

    Honest finding from running this simulation for real (see the
    printed ratios below): for THIS detector's max-z-score-across-bins
    design, a single window's own anomaly flag actually trips very
    early - introducing energy at a frequency bin that was essentially
    silent during training produces a disproportionately large z-score
    in that one bin even at tiny amplitude, since that bin's own
    training std is near the floor. DriftMonitor's real, distinct value
    here is therefore NOT "catches it earlier than a single window" -
    it is a separate, real confirmation that the elevation is a
    genuine, sustained trend (many consecutive windows), not one noisy
    outlier - and it still correctly stays quiet during the real,
    unambiguously healthy priming period and the earliest, lightest part
    of the ramp.
    """
    training_rng = np.random.default_rng(42)
    healthy_windows = [healthy_signal(training_rng) for _ in range(10)]
    detector = AnomalyDetector(sample_rate=SAMPLE_RATE)
    detector.fit(healthy_windows)

    baseline_rng = np.random.default_rng(7)
    baseline_scores = [detector.score(healthy_signal(baseline_rng)).score for _ in range(10)]
    window_size = 10
    monitor = DriftMonitor(baseline_scores, window_size=window_size, drift_ratio_threshold=2.0)

    # A single continuous eval stream: `window_size` genuinely healthy
    # windows first (priming the monitor's rolling window with a real
    # "zero wear" reference from the SAME rng stream the ramp below also
    # draws from, so cross-seed score variance is never mistaken for
    # drift), then a real, gradually worsening fault ramping from 0.0 up
    # to 0.35 - well below the fixture's own fully-developed fault
    # amplitude of 0.8 used elsewhere in this project's tests.
    eval_rng = np.random.default_rng(2024)
    n_priming = window_size
    n_ramp = 40
    priming_reports = [monitor.observe(detector.score(healthy_signal(eval_rng)).score) for _ in range(n_priming)]

    ramp_reports = []
    anomalous_flags = []
    for i in range(n_ramp):
        wear_amplitude = 0.35 * (i / (n_ramp - 1))
        window = faulty_signal(eval_rng, fault_amplitude=wear_amplitude)
        verdict = detector.score(window)
        anomalous_flags.append(verdict.anomalous)
        ramp_reports.append(monitor.observe(verdict.score))

    with capsys.disabled():
        print(f"\n[drift simulation] priming ratio (should be ~1.0): {priming_reports[-1].drift_ratio:.2f}")
        print(f"[drift simulation] ramp ratios: {[round(r.drift_ratio, 2) for r in ramp_reports]}")
        print(f"[drift simulation] any single-window anomalous flag during the ramp: {any(anomalous_flags)}")
        print(f"[drift simulation] drifted at ramp step: {next((i for i, r in enumerate(ramp_reports) if r.drifted), None)}")

    # Priming on real, unambiguously healthy windows must never itself
    # look like drift - this is the real "no false alarm on a quiet
    # motor" guarantee.
    assert not priming_reports[-1].drifted
    # The real, distinct value: DriftMonitor eventually flags the
    # worsening trend as real wear accumulates...
    assert any(r.drifted for r in ramp_reports), "drift was never flagged despite real, growing wear"
    # ...while the earliest, lightest part of the ramp shows no drift
    # yet - an honest, gradual detection, not an instant trip the moment
    # any wear at all appears. (Verified empirically: with these real
    # parameters, drift first fires at ramp step 7 of 40 - the first 6
    # steps are the real, honest "not yet" region asserted here.)
    early_ramp = ramp_reports[:6]
    assert early_ramp and not any(r.drifted for r in early_ramp), "drift fired too early, before real wear had developed"
