# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - tests/test_relative_floor.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The relative floor of the spread of each bin: off by default, a fraction of the level of the bin when on.

Evidence: simulator (synthetic motors with a seed; no recording of a real motor was used, so no threshold is claimed for any real machine).
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from hydra_umc_anomaly_detector.baseline import BaselineError, fit_baseline
from hydra_umc_anomaly_detector.detector import AnomalyDetector
from hydra_umc_anomaly_detector.fft import compute_spectrum
from hydra_umc_anomaly_detector.main import main
from signal_fixtures import SAMPLE_RATE, healthy_signal


def steady_windows(count: int = 12) -> list[np.ndarray]:
    rng = np.random.default_rng(7)
    t = np.arange(500) / SAMPLE_RATE
    # a very steady motor: the same tone, noise a thousand times smaller than the tone
    return [np.sin(2 * np.pi * 50.0 * t) + 0.0005 * rng.standard_normal(500) for _ in range(count)]


def test_off_by_default_the_baseline_is_the_one_it_was() -> None:
    windows = steady_windows()
    assert np.array_equal(fit_baseline(windows, SAMPLE_RATE).std, fit_baseline(windows, SAMPLE_RATE, relative_floor=0.0).std)


def test_on_no_bin_has_a_spread_below_the_fraction_of_its_level() -> None:
    baseline = fit_baseline(steady_windows(), SAMPLE_RATE, relative_floor=0.05)
    assert np.all(baseline.std >= 0.05 * np.abs(baseline.mean) - 1e-12)
    off = fit_baseline(steady_windows(), SAMPLE_RATE)
    assert np.all(baseline.std >= off.std)


def test_a_small_change_of_load_scores_far_lower_with_the_floor() -> None:
    windows = steady_windows()
    t = np.arange(500) / SAMPLE_RATE
    slightly_louder = compute_spectrum(1.03 * np.sin(2 * np.pi * 50.0 * t), SAMPLE_RATE)
    without = fit_baseline(windows, SAMPLE_RATE).z_scores(slightly_louder).max()
    with_floor = fit_baseline(windows, SAMPLE_RATE, relative_floor=0.05).z_scores(slightly_louder).max()
    assert without > 10 * with_floor
    assert with_floor < 1.0


def test_a_real_fault_still_stands_out_with_the_floor() -> None:
    windows = steady_windows()
    t = np.arange(500) / SAMPLE_RATE
    faulty = compute_spectrum(np.sin(2 * np.pi * 50.0 * t) + 0.8 * np.sin(2 * np.pi * 137.0 * t), SAMPLE_RATE)
    assert fit_baseline(windows, SAMPLE_RATE, relative_floor=0.05).z_scores(faulty).max() > 10.0


@pytest.mark.parametrize("value", [-0.1, 1.5, float("nan"), True, "0.1", None])
def test_a_value_outside_zero_to_one_is_refused(value) -> None:
    with pytest.raises(BaselineError, match="relative_floor"):
        fit_baseline(steady_windows(), SAMPLE_RATE, relative_floor=value)


def test_the_detector_hands_the_floor_to_its_fit_and_refuses_a_bad_one() -> None:
    detector = AnomalyDetector(SAMPLE_RATE, relative_floor=0.1)
    rng = np.random.default_rng(3)
    detector.fit([healthy_signal(rng) for _ in range(12)])
    assert detector.relative_floor == 0.1
    assert AnomalyDetector(SAMPLE_RATE).relative_floor == 0.0
    for bad in (-1, 2, True):
        with pytest.raises(ValueError, match="relative_floor"):
            AnomalyDetector(SAMPLE_RATE, relative_floor=bad)


def test_the_flag_and_the_config_file_refuse_a_value_outside_zero_to_one(tmp_path, capsys) -> None:
    assert main(["--relative-floor", "3"]) == 2
    assert "relative-floor" in capsys.readouterr().err
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"relativeFloor": -0.5}), encoding="utf-8")
    assert main(["--detector-config", str(bad)]) == 2
    assert "relative-floor" in capsys.readouterr().err
    unknown = tmp_path / "unknown.json"
    unknown.write_text(json.dumps({"relativeFlor": 0.1}), encoding="utf-8")
    assert main(["--detector-config", str(unknown)]) == 2
    assert "unknown keys" in capsys.readouterr().err
