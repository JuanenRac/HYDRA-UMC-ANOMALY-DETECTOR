# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - tests/test_baseline.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
from __future__ import annotations

import numpy as np
import pytest

from hydra_umc_anomaly_detector.baseline import BaselineError, fit_baseline, load_baseline, save_baseline
from hydra_umc_anomaly_detector.fft import compute_spectrum


def _sine(freq_hz: float, sample_rate: float, n_samples: int) -> np.ndarray:
    t = np.arange(n_samples) / sample_rate
    return np.sin(2 * np.pi * freq_hz * t)


def test_fit_baseline_needs_at_least_two_windows() -> None:
    with pytest.raises(BaselineError):
        fit_baseline([_sine(50.0, 1000.0, 200)], sample_rate=1000.0)


def test_fit_baseline_rejects_mismatched_window_lengths() -> None:
    windows = [_sine(50.0, 1000.0, 200), _sine(50.0, 1000.0, 300)]
    with pytest.raises(BaselineError):
        fit_baseline(windows, sample_rate=1000.0)


def test_fit_baseline_identical_windows_give_near_zero_z_score() -> None:
    sample_rate = 1000.0
    n = 200
    windows = [_sine(50.0, sample_rate, n) for _ in range(5)]
    baseline = fit_baseline(windows, sample_rate)

    same_spectrum = compute_spectrum(windows[0], sample_rate)
    z = baseline.z_scores(same_spectrum)

    # Every window was identical, so this signal IS the mean exactly -
    # z-scores must be genuinely ~0 everywhere, not just "small".
    assert np.allclose(z, 0.0, atol=1e-6)


def test_z_scores_rejects_bin_count_mismatch() -> None:
    sample_rate = 1000.0
    windows = [_sine(50.0, sample_rate, 200) for _ in range(3)]
    baseline = fit_baseline(windows, sample_rate)

    wrong_length_spectrum = compute_spectrum(_sine(50.0, sample_rate, 400), sample_rate)
    with pytest.raises(BaselineError):
        baseline.z_scores(wrong_length_spectrum)


def test_min_std_floor_prevents_division_by_zero() -> None:
    # Every training window is bit-for-bit identical -> real std would be
    # exactly 0 in every bin without the floor, and z_scores() would
    # divide by zero the moment a live reading differs at all.
    sample_rate = 1000.0
    windows = [_sine(50.0, sample_rate, 200) for _ in range(4)]
    baseline = fit_baseline(windows, sample_rate)
    assert np.all(baseline.std > 0.0)

    different = compute_spectrum(_sine(80.0, sample_rate, 200), sample_rate)
    z = baseline.z_scores(different)
    assert np.all(np.isfinite(z))  # no inf/nan from a zero-division


# ---------------------------------------------------------------------------
# save_baseline() / load_baseline() - the fitted baseline used to live
# only in memory, so a real service restart lost it.
# ---------------------------------------------------------------------------


def test_save_and_load_baseline_round_trips_exactly(tmp_path) -> None:
    sample_rate = 1000.0
    windows = [_sine(50.0, sample_rate, 200) for _ in range(5)]
    baseline = fit_baseline(windows, sample_rate)
    path = tmp_path / "baseline.npz"

    save_baseline(path, baseline, model_version=3)
    restored, model_version = load_baseline(path)

    assert model_version == 3
    assert np.array_equal(restored.freqs, baseline.freqs)
    assert np.array_equal(restored.mean, baseline.mean)
    assert np.array_equal(restored.std, baseline.std)
    assert restored.n_windows == 5
    assert restored.sample_rate == sample_rate


def test_save_baseline_never_appends_an_npz_suffix_of_its_own(tmp_path) -> None:
    # np.savez() silently appends .npz to a bare path/string - this must
    # write to EXACTLY the given path, not a caller-surprising sibling
    # file, since a real --baseline-path is what a caller expects back
    # from load_baseline() too.
    sample_rate = 1000.0
    windows = [_sine(50.0, sample_rate, 200) for _ in range(3)]
    baseline = fit_baseline(windows, sample_rate)
    path = tmp_path / "baseline.dat"

    save_baseline(path, baseline, model_version=1)

    assert path.is_file()
    assert not (tmp_path / "baseline.dat.npz").exists()


def test_load_baseline_raises_a_clear_error_for_a_missing_file(tmp_path) -> None:
    with pytest.raises(BaselineError):
        load_baseline(tmp_path / "does-not-exist.npz")


def test_load_baseline_raises_a_clear_error_for_a_corrupt_file(tmp_path) -> None:
    path = tmp_path / "corrupt.npz"
    path.write_bytes(b"not a real npz file")
    with pytest.raises(BaselineError):
        load_baseline(path)


def test_baseline_saved_without_provenance_loads_as_unknown(tmp_path) -> None:
    path = tmp_path / "old.npz"
    freqs = np.arange(3, dtype=float)
    with open(path, "wb") as fh:
        np.savez(fh, freqs=freqs, mean=freqs, std=freqs + 1, model_version=np.asarray(2))

    restored, model_version = load_baseline(path)

    assert model_version == 2
    assert restored.n_windows == 0
    assert restored.sample_rate == 0.0


def test_save_baseline_leaves_no_temporary_file_behind(tmp_path) -> None:
    sample_rate = 1000.0
    baseline = fit_baseline([_sine(50.0, sample_rate, 200) for _ in range(3)], sample_rate)
    path = tmp_path / "baseline.npz"

    save_baseline(path, baseline, model_version=1)

    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["baseline.npz"]


def test_save_baseline_replaces_an_existing_file_with_the_new_one(tmp_path) -> None:
    sample_rate = 1000.0
    path = tmp_path / "baseline.npz"
    save_baseline(path, fit_baseline([_sine(50.0, sample_rate, 200) for _ in range(3)], sample_rate), model_version=1)
    save_baseline(path, fit_baseline([_sine(80.0, sample_rate, 200) for _ in range(4)], sample_rate), model_version=2)

    restored, model_version = load_baseline(path)

    assert model_version == 2
    assert restored.n_windows == 4


def test_a_save_that_fails_midway_keeps_the_previous_baseline(tmp_path, monkeypatch) -> None:
    # a power cut or a full disk in the middle of the write used to leave a truncated
    # file that load_baseline() refused, losing the last good baseline with it
    sample_rate = 1000.0
    path = tmp_path / "baseline.npz"
    save_baseline(path, fit_baseline([_sine(50.0, sample_rate, 200) for _ in range(3)], sample_rate), model_version=1)
    before = path.read_bytes()

    def interrupted(fh, **arrays):
        fh.write(b"PK\x03\x04 half of a new baseline")
        raise OSError("disk full")

    monkeypatch.setattr(np, "savez", interrupted)
    with pytest.raises(OSError):
        save_baseline(path, fit_baseline([_sine(80.0, sample_rate, 200) for _ in range(4)], sample_rate), model_version=2)
    monkeypatch.undo()

    assert path.read_bytes() == before
    restored, model_version = load_baseline(path)
    assert model_version == 1 and restored.n_windows == 3
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["baseline.npz"]


@pytest.mark.strict_baseline
def test_the_default_baseline_needs_ten_windows_and_uses_the_sample_spread() -> None:
    windows = [_sine(50.0, 1000.0, 200) * (1.0 + 0.1 * k) for k in range(10)]
    with pytest.raises(BaselineError, match="at least 10"):
        fit_baseline(windows[:9], sample_rate=1000.0)
    default = fit_baseline(windows, sample_rate=1000.0)
    legacy = fit_baseline(windows, sample_rate=1000.0, min_windows=2, ddof=0)
    assert default.n_windows == 10
    peak = int(np.argmax(default.std))
    assert default.std[peak] == pytest.approx(legacy.std[peak] * np.sqrt(10 / 9))


@pytest.mark.strict_baseline
def test_the_legacy_limits_are_still_available_and_the_detector_flag_selects_them() -> None:
    from hydra_umc_anomaly_detector.detector import AnomalyDetector

    windows = [_sine(50.0, 1000.0, 200), _sine(50.0, 1000.0, 200) * 1.1]
    with pytest.raises(BaselineError):
        AnomalyDetector(1000.0).fit(windows)
    legacy = AnomalyDetector(1000.0, legacy_baseline=True)
    legacy.fit(windows)
    assert legacy.is_fitted and legacy.legacy_baseline
    with pytest.raises(BaselineError, match="min_windows"):
        fit_baseline(windows, sample_rate=1000.0, min_windows=1)
    with pytest.raises(BaselineError, match="ddof"):
        fit_baseline(windows, sample_rate=1000.0, ddof=2)


@pytest.mark.strict_baseline
def test_detector_min_windows_and_ddof_from_settings():
    import numpy as np
    from hydra_umc_anomaly_detector.detector import AnomalyDetector

    rng = np.random.default_rng(1)
    windows = [rng.normal(size=256) for _ in range(4)]
    detector = AnomalyDetector(1000.0, min_windows=3, ddof=0)
    detector.fit(windows)
    assert detector.is_fitted
    strict = AnomalyDetector(1000.0)
    with pytest.raises(BaselineError):
        strict.fit(windows)


def test_main_rejects_bad_detector_config(tmp_path):
    from hydra_umc_anomaly_detector.main import main

    bad = tmp_path / "d.json"
    bad.write_text('{"nope": 1}', encoding="utf-8")
    assert main(["--addr", "127.0.0.1", "--port", "0", "--detector-config", str(bad)]) == 2
    assert main(["--addr", "127.0.0.1", "--port", "0", "--detector-config", str(tmp_path / "missing.json")]) == 2
