# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - tests/test_no_signal_and_stale.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""A window with no signal is not normal, and a series that stops being scored does not keep its last verdict. Synthetic signals (simulator evidence)."""
import json

import numpy as np
import pytest

from hydra_umc_anomaly_detector.baseline import ac_rms, load_baseline, save_baseline
from hydra_umc_anomaly_detector.detector import AnomalyDetector
from hydra_umc_anomaly_detector.follower import Follower, parse_follow_config, verdict_of
from hydra_umc_anomaly_detector.registry import DetectorRegistry

RATE = 100.0


def healthy(count=12, size=200):
    rng = np.random.default_rng(7)
    t = np.arange(size) / RATE
    return [9.81 + np.sin(2 * np.pi * 10 * t) + rng.normal(0, 0.05, size) for _ in range(count)]


def fitted(**kwargs):
    detector = AnomalyDetector(RATE, **kwargs)
    detector.fit(healthy())
    return detector


def test_a_constant_reading_is_no_signal_whatever_its_level():
    detector = fitted()
    assert ac_rms(np.full(200, 9.81)) == pytest.approx(0.0, abs=1e-9)
    assert detector.score(np.full(200, 9.81)).no_signal is True
    assert detector.score(healthy(1)[0]).no_signal is False
    assert fitted(no_signal_fraction=0.0).score(np.full(200, 9.81)).no_signal is False


@pytest.mark.parametrize("value", [-0.1, 1.0, True, "0.1"])
def test_the_fraction_is_checked(value):
    with pytest.raises(ValueError, match="no_signal_fraction"):
        AnomalyDetector(RATE, no_signal_fraction=value)


def test_the_training_rms_is_saved_and_an_older_file_has_none(tmp_path):
    detector = fitted()
    path = tmp_path / "b.npz"
    save_baseline(path, detector.baseline, 1)
    loaded, _ = load_baseline(path)
    assert loaded.training_rms == pytest.approx(detector.baseline.training_rms)
    assert loaded.training_rms > 0.5
    with np.load(path) as data:
        older = {name: data[name] for name in data.files if name != "training_rms"}
    np.savez(tmp_path / "old.npz", **older)
    old, version = load_baseline(tmp_path / "old.npz")
    assert old.training_rms == 0.0
    again = AnomalyDetector(RATE)
    again.restore(old, version)
    assert again.score(np.full(200, 9.81)).no_signal is False


def test_the_word_of_a_silent_window():
    assert verdict_of(0.1, 5.0, False, no_signal=True) == "no_signal"
    assert verdict_of(50.0, 5.0, True, no_signal=True) == "anomalous"


class Publisher:
    def __init__(self):
        self.sent = []

    def publish(self, topic, payload, retain=False):
        self.sent.append((topic, json.loads(payload), retain))


class NothingArrives:
    def query(self, **_kwargs):
        return []


def following(clock, **extra):
    raw = {"datalake": {"url": "http://127.0.0.1:9"}, "series": [{"sourceId": "motor1", "kind": "vibration", "field": "x", "windowSize": 200}],
           "publishVerdicts": True, **extra}
    publisher = Publisher()
    follower = Follower(parse_follow_config(raw), DetectorRegistry(lambda: AnomalyDetector(RATE), None, 4), NothingArrives(), sample_rate=RATE,
                        publisher=publisher, clock=lambda: clock[0], log=lambda _text: None)
    return follower, publisher


def test_a_series_with_nothing_scored_becomes_stale_once():
    clock = [1000.0]
    follower, publisher = following(clock)
    follower.tick()
    clock[0] += 59.0
    follower.tick()
    assert publisher.sent == []
    clock[0] += 2.0
    follower.tick()
    follower.tick()
    assert len(publisher.sent) == 1
    topic, message, retain = publisher.sent[0]
    assert topic.endswith("/verdict/motor1/vibration/x") and retain is True
    assert message["schema"] == "anomaly-verdict/1" and message["verdict"] == "stale" and message["score"] == 0.0


def test_zero_seconds_never_goes_stale():
    clock = [1000.0]
    follower, publisher = following(clock, staleAfterS=0)
    follower.tick()
    clock[0] += 100000.0
    follower.tick()
    assert publisher.sent == []
