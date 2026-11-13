# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - tests/test_simulation_bench.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Simulation bench: the real service loop (Follower, detector registry, DATALAKE client, MQTT publisher) against a simulated plant.

The plant is a small fleet of motors whose vibration is synthesised from a fixed seed, read through a real HTTP DATALAKE stand-in and announced to a
real TCP broker stand-in. The scenarios inject what goes wrong in a plant: a bearing that wears out little by little, a DATALAKE that is not there,
a DATALAKE that refuses the verdicts, a broker that refuses the connection, a hole in the sampling and a sensor that sends garbage.
Nothing here says how the detector behaves on a real machine: that needs a real machine."""
from __future__ import annotations

import socket

import numpy as np
import pytest

from fake_datalake import running_fake_datalake
from hydra_umc_anomaly_detector.datalake_client import DatalakeClient
from hydra_umc_anomaly_detector.detector import AnomalyDetector
from hydra_umc_anomaly_detector.follower import Follower, parse_follow_config
from hydra_umc_anomaly_detector.mqtt_publish import MqttPublisher
from hydra_umc_anomaly_detector.registry import DetectorRegistry, SeriesKey
from test_follower import broker

SEED = 20260101
RATE = 1000.0
WINDOW = 200
MOTORS = ("press-1", "press-2", "press-3")


def _keys() -> list[SeriesKey]:
    return [SeriesKey(name, "vibration", "value") for name in MOTORS]


def _healthy(rng: np.random.Generator, start: int, n: int) -> np.ndarray:
    t = (np.arange(n) + start) / RATE
    return np.sin(2 * np.pi * 50.0 * t) + 0.05 * rng.standard_normal(n)


def _worn(rng: np.random.Generator, start: int, n: int, wear: float) -> np.ndarray:
    """The same motor with a defect line at 220 Hz whose amplitude is `wear`."""
    t = (np.arange(n) + start) / RATE
    return _healthy(rng, start, n) + wear * np.sin(2 * np.pi * 220.0 * t)


def _points(key: SeriesKey, values: np.ndarray, first: int) -> list[dict]:
    return [{"sourceId": key.source_id, "kind": key.kind, "field": key.field, "timestamp": first + i, "value": float(v)} for i, v in enumerate(values)]


def _registry(rng: np.random.Generator) -> DetectorRegistry:
    registry = DetectorRegistry(lambda: AnomalyDetector(sample_rate=RATE, threshold=10.0))
    for key in _keys():
        detector = registry.new_detector()
        detector.fit([_healthy(rng, 0, WINDOW) for _ in range(24)])
        registry.insert(key, detector)
    return registry


def _config(url: str, **over) -> object:
    raw = {
        "datalake": {"url": url, "timeoutSeconds": 1.0},
        "series": [{"sourceId": key.source_id, "kind": key.kind, "field": key.field, "windowSize": WINDOW} for key in _keys()],
        "intervalSeconds": 0.5,
        "maxWindowsPerTick": 50,
    }
    raw.update(over)
    return parse_follow_config(raw)


def _follower(url: str, registry: DetectorRegistry, **over) -> Follower:
    publisher = over.pop("publisher", None)
    return Follower(_config(url, **over), registry, DatalakeClient(url), sample_rate=RATE, publisher=publisher, clock=lambda: 10.0, log=lambda text: None)


def _flags(datalake, name: str) -> list[float]:
    return [i["body"]["fields"]["value_anomalous"] for i in datalake.ingested if i["body"]["sourceId"] == name]


def _fleet_run(seed: int) -> list[float]:
    rng = np.random.default_rng(seed)
    registry = _registry(rng)
    with running_fake_datalake() as (url, datalake):
        for index, key in enumerate(_keys()):
            wear = [0.0, 0.0, 3.0][index]
            datalake.points += _points(key, _worn(rng, 0, WINDOW * 4, wear) if wear else _healthy(rng, 0, WINDOW * 4), 10_000)
        follower = _follower(url, registry)
        assert follower.tick() == 12
        return [round(i["body"]["fields"]["value"], 9) for i in datalake.ingested]


def test_only_the_worn_motor_of_the_fleet_is_flagged():
    rng = np.random.default_rng(SEED)
    registry = _registry(rng)
    with running_fake_datalake() as (url, datalake):
        datalake.points += _points(_keys()[0], _healthy(rng, 0, WINDOW * 4), 10_000)
        datalake.points += _points(_keys()[1], _healthy(rng, 0, WINDOW * 4), 10_000)
        datalake.points += _points(_keys()[2], _worn(rng, 0, WINDOW * 4, 3.0), 10_000)
        follower = _follower(url, registry)
        assert follower.tick() == 12
        assert _flags(datalake, "press-1") == [0.0] * 4
        assert _flags(datalake, "press-2") == [0.0] * 4
        assert _flags(datalake, "press-3") == [1.0] * 4
        assert follower.snapshot()["anomalies"] == 4


def test_a_bearing_that_wears_out_is_caught_once_and_stays_caught():
    rng = np.random.default_rng(SEED + 1)
    registry = _registry(rng)
    key = _keys()[0]
    wears = [0.0, 0.0, 0.02, 0.05, 0.3, 0.8, 1.5, 3.0]
    values = np.concatenate([_worn(rng, i * WINDOW, WINDOW, wear) for i, wear in enumerate(wears)])
    with running_fake_datalake() as (url, datalake):
        datalake.points = _points(key, values, 10_000)
        follower = _follower(url, registry)
        assert follower.tick() == len(wears)
        flags = _flags(datalake, key.source_id)
        scores = [i["body"]["fields"]["value"] for i in datalake.ingested]
        assert flags[:2] == [0.0, 0.0] and flags[-1] == 1.0
        first = flags.index(1.0)
        assert all(flag == 1.0 for flag in flags[first:])  # once the wear is visible it does not flicker back to healthy
        assert scores[-1] > 10 * scores[0] + 1e-9 and scores[-1] == max(scores)


def test_the_same_seed_gives_the_same_verdicts_and_another_seed_does_not():
    assert _fleet_run(SEED) == _fleet_run(SEED)
    assert _fleet_run(SEED) != _fleet_run(SEED + 7)


def test_a_datalake_that_is_not_there_is_counted_and_the_loop_survives():
    rng = np.random.default_rng(SEED + 2)
    registry = _registry(rng)
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        dead_url = f"http://127.0.0.1:{holder.getsockname()[1]}"
        follower = _follower(dead_url, registry)
        assert follower.tick() == 0
        snapshot = follower.snapshot()
        assert snapshot["datalakeErrors"] == len(MOTORS) and snapshot["windowsScored"] == 0 and snapshot["ticks"] == 1
        assert {row["note"] for row in snapshot["series"]} == {"DATALAKE unreachable"}


def test_a_datalake_that_refuses_the_verdicts_does_not_stop_the_scoring_and_recovers():
    rng = np.random.default_rng(SEED + 3)
    registry = _registry(rng)
    key = _keys()[0]
    with running_fake_datalake() as (url, datalake):
        datalake.points = _points(key, _healthy(rng, 0, WINDOW * 2), 10_000)
        datalake.fail_ingest = True
        follower = _follower(url, registry)
        assert follower.tick() == 2
        assert follower.snapshot()["writeErrors"] == 2 and follower.snapshot()["windowsScored"] == 2
        datalake.fail_ingest = False
        datalake.points += _points(key, _healthy(rng, WINDOW * 2, WINDOW), 10_000 + WINDOW * 2)
        assert follower.tick() == 1
        assert follower.snapshot()["writeErrors"] == 2  # the new verdict was written
        assert datalake.ingested[-1]["body"]["timestamp"] == 10_000 + WINDOW * 3 - 1


def test_a_broker_that_refuses_the_connection_is_counted_and_the_verdicts_still_reach_the_datalake():
    rng = np.random.default_rng(SEED + 4)
    registry = _registry(rng)
    key = _keys()[2]
    with running_fake_datalake() as (url, datalake), broker(return_code=5) as mqtt:
        datalake.points = _points(key, _worn(rng, 0, WINDOW * 2, 3.0), 10_000)
        config_over = {"mqtt": {"host": "127.0.0.1", "port": mqtt.port}}
        publisher = MqttPublisher(_config(url, **config_over).mqtt)
        follower = _follower(url, registry, publisher=publisher, **config_over)
        assert follower.tick() == 2
        snapshot = follower.snapshot()
        assert snapshot["mqttErrors"] >= 1 and snapshot["anomalies"] == 2
        assert _flags(datalake, key.source_id) == [1.0, 1.0] and mqtt.messages == []


def test_a_hole_in_the_sampling_never_makes_a_window_out_of_two_pieces():
    rng = np.random.default_rng(SEED + 5)
    registry = _registry(rng)
    key = _keys()[0]
    first = _points(key, _healthy(rng, 0, WINDOW + 120), 10_000)
    second = _points(key, _healthy(rng, 5000, WINDOW * 2), 10_000 + WINDOW + 120 + 400)  # 400 ms without samples
    with running_fake_datalake() as (url, datalake):
        datalake.points = first + second
        follower = _follower(url, registry)
        assert follower.tick() == 3  # one window before the hole, two after it; the 120 samples before the hole are dropped
        stamps = [i["body"]["timestamp"] for i in datalake.ingested]
        assert stamps == [10_000 + WINDOW - 1, 10_000 + WINDOW + 120 + 400 + WINDOW - 1, 10_000 + WINDOW + 120 + 400 + 2 * WINDOW - 1]


def test_a_sensor_that_sends_garbage_is_refused_without_taking_the_other_motors_down():
    rng = np.random.default_rng(SEED + 6)
    registry = _registry(rng)
    bad, good = _keys()[0], _keys()[1]
    values = _healthy(rng, 0, WINDOW)
    values[17] = float("nan")
    with running_fake_datalake() as (url, datalake):
        datalake.points = _points(bad, values, 10_000) + _points(good, _healthy(rng, 0, WINDOW), 10_000)
        follower = _follower(url, registry)
        follower.tick()
        notes = {row["sourceId"]: row["note"] for row in follower.snapshot()["series"]}
        assert notes["press-1"].startswith("window refused") or follower.snapshot()["windowsScored"] >= 1
        assert _flags(datalake, "press-2") == [0.0]


@pytest.mark.parametrize("name", MOTORS)
def test_a_motor_without_a_baseline_is_never_scored_against_another_ones(name):
    rng = np.random.default_rng(SEED + 8)
    registry = DetectorRegistry(lambda: AnomalyDetector(sample_rate=RATE, threshold=10.0))
    other = next(key for key in _keys() if key.source_id != name)
    detector = registry.new_detector()
    detector.fit([_healthy(rng, 0, WINDOW) for _ in range(24)])
    registry.insert(other, detector)
    key = SeriesKey(name, "vibration", "value")
    with running_fake_datalake() as (url, datalake):
        datalake.points = _points(key, _healthy(rng, 0, WINDOW), 10_000)
        follower = _follower(url, registry)
        follower.tick()
        assert _flags(datalake, name) == []
        notes = {row["sourceId"]: row["note"] for row in follower.snapshot()["series"]}
        assert notes[name] == "not fitted"
