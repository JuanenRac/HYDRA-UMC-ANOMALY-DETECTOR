# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - tests/test_robust_scoring.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The robust scoring: log magnitudes normalised by the RMS, median and spread, bands, and a threshold found with held-out healthy windows."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from hydra_umc_anomaly_detector.baseline import BaselineError, fit_robust_baseline, load_baseline, save_baseline
from hydra_umc_anomaly_detector.detector import AnomalyDetector, NotFittedError
from hydra_umc_anomaly_detector.main import main
from hydra_umc_anomaly_detector.robust import (
    RobustError,
    RobustSettings,
    features,
    parse_robust_settings,
    score_features,
    split_windows,
)
from realistic_signals import Machine

RATE = 1000.0


def _detector(window: str = "none", **settings: object) -> AnomalyDetector:
    return AnomalyDetector(RATE, window=window, scoring="robust", robust=RobustSettings(**settings))  # type: ignore[arg-type]


def _wandering() -> Machine:
    return Machine(13, speed_jitter=0.05, load_variation=0.4, noise=0.3)


# ---------------------------------------------------------------------------------------------------------------- features


def test_the_features_do_not_depend_on_the_level_of_the_signal() -> None:
    signal = Machine(1).healthy()
    _, plain = features(signal, RATE, "none", RobustSettings())
    _, louder = features(signal * 7.5, RATE, "none", RobustSettings())
    assert np.allclose(plain, louder, atol=1e-9)
    _, raw = features(signal, RATE, "none", RobustSettings(normalize=False))
    _, raw_louder = features(signal * 10, RATE, "none", RobustSettings(normalize=False))
    assert np.allclose(raw_louder - raw, 1.0)  # without the normalisation a tenfold signal is one unit more in the logarithm, at every bin


def test_a_silent_or_flat_window_does_not_divide_by_zero() -> None:
    freqs, flat = features(np.zeros(256), RATE, "none", RobustSettings())
    assert len(freqs) == 128 and np.isfinite(flat).all()
    _, constant = features(np.full(256, 3.0), RATE, "hann", RobustSettings())
    assert np.isfinite(constant).all()


def test_a_band_scores_the_root_of_the_mean_of_squares_and_up_ignores_nulls() -> None:
    z = np.array([0.0, 0.0, -9.0, 0.0, 3.0, 4.0, 0.0, 0.0])
    assert score_features(z, 0, "both") == (9.0, 2)
    assert score_features(z, 0, "up") == (4.0, 5)  # a deep null is not a fault
    score, index = score_features(z, 2, "up")  # bands [0 0 -9 0] and [3 4 0 0]
    assert score == pytest.approx(np.sqrt((9 + 16) / 4)) and index == 5
    assert score_features(z, 2, "both")[0] == pytest.approx(np.sqrt(81 / 4))
    assert score_features(z, 100, "up")[0] == 4.0  # more bands than bins: one per bin


def test_the_split_holds_out_every_third_window() -> None:
    training, validation = split_windows(10, 0.3)
    assert validation == [2, 5, 8] and training == [0, 1, 3, 4, 6, 7, 9]
    assert split_windows(10, 0.5)[1] == [1, 3, 5, 7, 9]


# --------------------------------------------------------------------------------------------------------------- settings


@pytest.mark.parametrize("change", [
    {"quantile": 0.4}, {"quantile": 1.5}, {"margin": 0.5}, {"validation_fraction": 0.05}, {"validation_fraction": 0.9}, {"bands": 1}, {"bands": 300},
    {"bands": True}, {"min_windows": 5}, {"min_windows": True}, {"normalize": "yes"}, {"side": "down"}, {"energy_floor": 0}, {"min_scale": float("nan")},
])
def test_bad_settings_are_refused(change: dict) -> None:
    with pytest.raises(RobustError):
        RobustSettings(**change)


def test_the_settings_come_from_the_detector_configuration() -> None:
    settings = parse_robust_settings({"quantile": 0.95, "margin": 2, "validationFraction": 0.25, "bands": 8, "side": "both", "normalize": False, "minWindows": 30})
    assert (settings.quantile, settings.margin, settings.validation_fraction, settings.bands, settings.side, settings.normalize, settings.min_windows) == (0.95, 2, 0.25, 8, "both", False, 30)
    assert parse_robust_settings({}) == RobustSettings() and parse_robust_settings({}).describe()["minWindows"] == 20
    for bad in ([], {"unknown": 1}, {"quantile": "x"}):
        with pytest.raises(RobustError):
            parse_robust_settings(bad)
    with pytest.raises(ValueError, match="scoring"):
        AnomalyDetector(RATE, scoring="fuzzy")


# -------------------------------------------------------------------------------------------------------------------- fit


def test_the_fit_needs_enough_windows_to_hold_some_out_and_all_of_one_size() -> None:
    machine = Machine(2)
    with pytest.raises(BaselineError, match="at least 20"):
        _detector().fit(machine.healthy_windows(19))
    with pytest.raises(BaselineError, match="same length"):
        fit_robust_baseline([*machine.healthy_windows(20), np.zeros(128)], RATE, RobustSettings())
    with pytest.raises(BaselineError, match="finite"):
        fit_robust_baseline([*machine.healthy_windows(19), np.full(512, np.nan)], RATE, RobustSettings())
    with pytest.raises(BaselineError, match="leave"):
        fit_robust_baseline(machine.healthy_windows(11), RATE, RobustSettings(min_windows=11, validation_fraction=0.5))


def test_the_baseline_carries_its_threshold_and_what_it_was_made_with() -> None:
    detector = _detector(bands=8)
    detector.fit(Machine(3).healthy_windows(30))
    baseline = detector.baseline
    assert baseline is not None and baseline.mode == "robust" and baseline.threshold is not None and baseline.threshold > 0
    assert baseline.n_windows == 20 and baseline.validation_windows == 10 and baseline.settings is not None and baseline.settings.bands == 8


def test_the_threshold_is_the_quantile_of_the_held_out_scores_times_the_margin() -> None:
    windows = Machine(4).healthy_windows(30)
    loose = fit_robust_baseline(windows, RATE, RobustSettings(margin=1.0))
    wide = fit_robust_baseline(windows, RATE, RobustSettings(margin=2.0))
    assert wide.threshold == pytest.approx(2.0 * loose.threshold)  # type: ignore[operator]
    median = fit_robust_baseline(windows, RATE, RobustSettings(margin=1.0, quantile=0.5))
    assert median.threshold < loose.threshold  # type: ignore[operator]


# ----------------------------------------------------------------------------------------------------------------- score


def test_a_fault_is_found_and_a_healthy_machine_that_wanders_is_left_alone() -> None:
    machine = _wandering()
    detector = _detector()
    detector.fit(machine.healthy_windows(40))
    healthy = [detector.score(machine.healthy()) for _ in range(400)]
    faults = [detector.score(machine.faulty()) for _ in range(60)]
    assert np.mean([v.anomalous for v in healthy]) < 0.01
    assert all(v.anomalous for v in faults)
    assert min(v.score for v in faults) > detector.baseline.threshold  # type: ignore[union-attr]
    assert all(v.threshold == detector.baseline.threshold for v in healthy)  # type: ignore[union-attr]
    verdict = faults[0]
    assert 165 < verdict.worst_bin_freq < 181  # the bin of the fault (173 Hz)
    assert verdict.calibration_windows == detector.baseline.n_windows  # type: ignore[union-attr]


def test_the_aim_of_few_training_windows_is_met_by_the_robust_scoring() -> None:
    """The aim that the z-score does not meet with 20 windows (see test_realistic_false_positives): false alarms under one in a hundred, and every fault found."""
    machine = _wandering()
    detector = _detector()
    detector.fit(machine.healthy_windows(20))
    assert np.mean([detector.score(machine.healthy()).anomalous for _ in range(1500)]) < 0.02
    assert all(detector.score(machine.faulty()).anomalous for _ in range(100))


@pytest.mark.parametrize("window", ["hann", "welch"])
def test_the_robust_scoring_works_with_the_other_windows(window: str) -> None:
    machine = Machine(14, speed_jitter=0.03, load_variation=0.3, noise=0.2)
    detector = _detector(window)
    detector.fit(machine.healthy_windows(60))
    assert np.mean([detector.score(machine.healthy()).anomalous for _ in range(300)]) < 0.02
    assert np.mean([detector.score(machine.faulty()).anomalous for _ in range(60)]) > 0.9


def test_a_louder_healthy_machine_is_not_an_anomaly_but_the_original_scoring_says_it_is() -> None:
    machine = Machine(5, load_variation=0.05)
    windows = machine.healthy_windows(40)
    robust, original = _detector(), AnomalyDetector(RATE)
    robust.fit(windows)
    original.fit(windows)
    louder = machine.healthy() * 3.0
    assert not robust.score(louder).anomalous
    assert original.score(louder).anomalous


def test_a_window_of_another_size_is_refused_and_an_unfitted_detector_says_so() -> None:
    detector = _detector()
    with pytest.raises(NotFittedError):
        detector.score(np.zeros(512))
    detector.fit(Machine(6).healthy_windows(20))
    with pytest.raises(BaselineError, match="bins"):
        detector.score(np.sin(np.arange(256)))
    with pytest.raises(ValueError, match="finite"):
        detector.score([float("nan")] * 512)


# ---------------------------------------------------------------------------------------------------------- persistence


def test_the_saved_baseline_says_by_itself_how_it_must_be_used(tmp_path: Path) -> None:
    detector = _detector("hann", bands=4, margin=1.7, side="both")
    detector.fit(Machine(7).healthy_windows(30))
    path = tmp_path / "baseline.npz"
    save_baseline(path, detector.baseline, 3)  # type: ignore[arg-type]
    with np.load(path) as data:
        assert str(data["mode"]) == "robust" and float(data["threshold"]) == pytest.approx(detector.baseline.threshold)  # type: ignore[union-attr]
        assert int(data["bands"]) == 4 and float(data["margin"]) == pytest.approx(1.7) and str(data["side"]) == "both"
    restored, version = load_baseline(path)
    assert version == 3 and restored.mode == "robust" and restored.settings == detector.baseline.settings  # type: ignore[union-attr]
    assert np.array_equal(restored.mean, detector.baseline.mean) and restored.threshold == detector.baseline.threshold  # type: ignore[union-attr]
    other = _detector("hann", bands=4, margin=1.7, side="both")
    other.restore(restored, version)
    window = Machine(8).healthy()
    restored_verdict, fitted_verdict = other.score(window), detector.score(window)
    assert (restored_verdict.score, restored_verdict.anomalous, restored_verdict.worst_bin_freq, restored_verdict.threshold) == (fitted_verdict.score, fitted_verdict.anomalous, fitted_verdict.worst_bin_freq, fitted_verdict.threshold)  # the restored detector judges as the fitted one
    assert restored_verdict.model_version == 3


def test_a_baseline_of_the_other_scoring_is_not_restored(tmp_path: Path) -> None:
    robust = _detector()
    robust.fit(Machine(9).healthy_windows(20))
    original = AnomalyDetector(RATE)
    original.fit(Machine(9).healthy_windows(20))
    with pytest.raises(BaselineError, match="scoring"):
        original.restore(robust.baseline, 1)  # type: ignore[arg-type]
    with pytest.raises(BaselineError, match="scoring"):
        robust.restore(original.baseline, 1)  # type: ignore[arg-type]
    path = tmp_path / "old.npz"
    save_baseline(path, original.baseline, 1)  # type: ignore[arg-type]
    with np.load(path) as data:
        assert "mode" not in data.files  # the file of the original scoring is the file it always was
    assert load_baseline(path)[0].mode == "zscore"


# ------------------------------------------------------------------------------------------------------ configuration


def _serve_args(tmp_path: Path, config: dict) -> list[str]:
    path = tmp_path / "detector.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return ["--port", "0", "--detector-config", str(path)]


def test_a_bad_robust_configuration_stops_the_start(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert main(_serve_args(tmp_path, {"scoring": "fuzzy"})) == 2
    assert "scoring must be one of" in capsys.readouterr().err
    assert main(_serve_args(tmp_path, {"scoring": "robust", "robust": {"quantile": 2}})) == 2
    assert "quantile" in capsys.readouterr().err
    assert main(_serve_args(tmp_path, {"robust": {"bogus": 1}})) == 2
    assert "robust is an object" in capsys.readouterr().err


def test_the_stats_name_the_scoring_only_when_it_is_not_the_original() -> None:
    import threading
    import urllib.request

    from hydra_umc_anomaly_detector.api import DetectorServer

    for scoring, expected in (("zscore", {"fitted": False, "window": "none", "writeProtected": False}),
                              ("robust", {"fitted": False, "window": "none", "writeProtected": False, "scoring": "robust"})):
        server = DetectorServer(("127.0.0.1", 0), AnomalyDetector(RATE, scoring=scoring))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/stats", timeout=5) as response:
                assert json.loads(response.read()) == expected
        finally:
            server.shutdown()
            server.server_close()
