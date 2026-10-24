# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - tests/test_follower.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Continuous service mode, against a real HTTP DATALAKE stand-in and a real TCP broker stand-in: the points it reads, the verdicts it
writes back, the alarms it publishes, and what it does when either of them is not there."""
from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pytest

from fake_datalake import running_fake_datalake
from hydra_umc_anomaly_detector.datalake_client import DatalakeClient
from hydra_umc_anomaly_detector.detector import AnomalyDetector
from hydra_umc_anomaly_detector.follower import FollowConfigError, Follower, parse_follow_config
from hydra_umc_anomaly_detector.main import main
from hydra_umc_anomaly_detector.mqtt_publish import MqttPublisher, MqttTarget, connect_packet, encode_length, publish_packet
from hydra_umc_anomaly_detector.registry import DetectorRegistry, SeriesKey

RATE = 1000.0
WINDOW = 200
KEY = SeriesKey("motor-1", "vibration", "value")


def _sine(freq_hz: float, start_index: int, n: int = WINDOW, amplitude: float = 1.0) -> list[float]:
    t = (np.arange(n) + start_index) / RATE
    return list(amplitude * np.sin(2 * np.pi * freq_hz * t))


def _points(values: list[float], first_timestamp: int, key: SeriesKey = KEY, step_ms: int = 1) -> list[dict]:
    return [
        {"sourceId": key.source_id, "kind": key.kind, "field": key.field, "timestamp": first_timestamp + i * step_ms, "value": float(v)}
        for i, v in enumerate(values)
    ]


def _factory() -> AnomalyDetector:
    return AnomalyDetector(sample_rate=RATE, threshold=4.0)


def _registry_with_fitted_series() -> DetectorRegistry:
    registry = DetectorRegistry(_factory)
    detector = registry.new_detector()
    detector.fit([_sine(50.0, 0) for _ in range(6)])
    registry.insert(KEY, detector)
    return registry


def _config(url: str, **over) -> object:
    raw = {"datalake": {"url": url}, "series": [{"sourceId": KEY.source_id, "kind": KEY.kind, "field": KEY.field, "windowSize": WINDOW}], "intervalSeconds": 0.5}
    raw.update(over)
    return parse_follow_config(raw)


class FakeBroker:
    """Accepts one connection at a time, answers CONNECT with CONNACK and records what is published."""

    def __init__(self, return_code: int = 0) -> None:
        self.messages: list[tuple[str, bytes, bool]] = []
        self.return_code = return_code
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(5)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @staticmethod
    def _read_packet(connection: socket.socket) -> tuple[int, bytes] | None:
        first = connection.recv(1)
        if not first:
            return None
        multiplier, length = 1, 0
        while True:
            digit = connection.recv(1)[0]
            length += (digit & 127) * multiplier
            multiplier *= 128
            if not digit & 128:
                break
        data = b""
        while len(data) < length:
            chunk = connection.recv(length - len(data))
            if not chunk:
                break
            data += chunk
        return first[0], data

    def _serve(self) -> None:
        while True:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            with connection:
                connection.settimeout(3)
                try:
                    packet = self._read_packet(connection)
                    if packet is None or packet[0] != 0x10:
                        continue
                    connection.sendall(bytes([0x20, 0x02, 0x00, self.return_code]))
                    while True:
                        packet = self._read_packet(connection)
                        if packet is None or packet[0] == 0xE0:
                            break
                        if packet[0] & 0xF0 == 0x30:
                            topic_length = int.from_bytes(packet[1][:2], "big")
                            topic = packet[1][2:2 + topic_length].decode()
                            self.messages.append((topic, packet[1][2 + topic_length:], bool(packet[0] & 1)))
                except (OSError, IndexError):
                    continue

    def close(self) -> None:
        self.listener.close()


@contextmanager
def broker(**kwargs) -> Iterator[FakeBroker]:
    b = FakeBroker(**kwargs)
    try:
        yield b
    finally:
        b.close()


# ---------------------------------------------------------------------------------------------------------- the configuration
def test_a_good_configuration_gets_its_defaults():
    config = _config("http://127.0.0.1:8095")
    assert config.score_kind == "anomaly_score" and config.write_scores is True and config.mqtt is None
    assert config.series[0].key == KEY and config.series[0].window_size == WINDOW


@pytest.mark.parametrize("change", [
    {"datalake": {"url": "ftp://x"}},
    {"datalake": {}},
    {"series": []},
    {"series": [{"sourceId": "a b", "kind": "k", "field": "f", "windowSize": 64}]},
    {"series": [{"sourceId": "a", "kind": "k", "field": "f", "windowSize": 3}]},
    {"series": [{"sourceId": "a", "kind": "k", "field": "f", "windowSize": 64}, {"sourceId": "a", "kind": "k", "field": "f", "windowSize": 64}]},
    {"intervalSeconds": 0},
    {"intervalSeconds": "5"},
    {"maxWindowsPerTick": 0},
    {"scoreKind": "bad kind"},
    {"topicPrefix": "a/#"},
    {"topicPrefix": "a/"},
    {"mqtt": {}},
    {"mqtt": {"host": "broker.example.com"}},
    {"mqtt": {"host": "127.0.0.1", "port": 70000}},
    {"mqtt": {"host": "127.0.0.1", "usernameFile": "/does/not/exist"}},
])
def test_a_bad_configuration_is_refused_with_the_key_that_is_wrong(change):
    raw = {"datalake": {"url": "http://127.0.0.1:8095"}, "series": [{"sourceId": "a", "kind": "k", "field": "f", "windowSize": 64}]}
    raw.update(change)
    with pytest.raises(FollowConfigError):
        parse_follow_config(raw)


def test_a_broker_off_this_machine_needs_tls_or_a_stated_choice():
    raw = {"datalake": {"url": "http://127.0.0.1:8095"}, "series": [{"sourceId": "a", "kind": "k", "field": "f", "windowSize": 64}]}
    assert parse_follow_config({**raw, "mqtt": {"host": "broker.example.com", "tls": True}}).mqtt.port == 8883
    assert parse_follow_config({**raw, "mqtt": {"host": "broker.example.com", "allowPlaintext": True}}).mqtt.port == 1883
    assert parse_follow_config({**raw, "mqtt": {"host": "localhost"}}).mqtt.tls is False


# ---------------------------------------------------------------------------------------------------------- the packets
def test_mqtt_lengths_and_packets_are_encoded_as_the_standard_says():
    assert encode_length(0) == b"\x00" and encode_length(127) == b"\x7f" and encode_length(128) == b"\x80\x01" and encode_length(16384) == b"\x80\x80\x01"
    connect = connect_packet("cid", "u", "p")
    assert connect[0] == 0x10 and b"MQTT\x04" in connect and connect[connect.index(b"MQTT") + 5] == 0xC2
    assert publish_packet("a/b", b"x") == b"\x30\x06\x00\x03a/bx"
    assert publish_packet("a/b", b"x", retain=True)[0] == 0x31


def test_a_topic_with_a_wildcard_is_never_published():
    with pytest.raises(Exception):
        publish_packet("a/+/b", b"x")


# ---------------------------------------------------------------------------------------------------------- following a series
def test_a_healthy_series_is_scored_window_by_window_and_the_verdicts_are_written_back():
    with running_fake_datalake() as (url, datalake):
        datalake.points = _points(_sine(50.0, 0, WINDOW * 3 + 50), 10_000)
        follower = Follower(_config(url), _registry_with_fitted_series(), DatalakeClient(url), sample_rate=RATE, clock=lambda: 10.05)
        assert follower.tick() == 3
        assert [i["body"]["timestamp"] for i in datalake.ingested] == [10_000 + WINDOW - 1, 10_000 + 2 * WINDOW - 1, 10_000 + 3 * WINDOW - 1]
        first = datalake.ingested[0]["body"]
        assert first["kind"] == "anomaly_score" and first["sourceId"] == KEY.source_id
        assert set(first["fields"]) == {"value", "value_anomalous", "value_model"} and first["fields"]["value_anomalous"] == 0.0
        # the 50 samples left do not make a window; nothing new arrives, so nothing is scored and nothing is scored twice
        assert follower.tick() == 0 and len(datalake.ingested) == 3
        snapshot = follower.snapshot()
        assert snapshot["windowsScored"] == 3 and snapshot["series"][0]["anomalous"] is False


def test_new_points_continue_where_the_last_window_ended():
    with running_fake_datalake() as (url, datalake):
        datalake.points = _points(_sine(50.0, 0, WINDOW + 20), 10_000)
        follower = Follower(_config(url), _registry_with_fitted_series(), DatalakeClient(url), sample_rate=RATE, clock=lambda: 10.0)
        assert follower.tick() == 1
        datalake.points += _points(_sine(50.0, WINDOW + 20, WINDOW), 10_000 + WINDOW + 20)
        assert follower.tick() == 1
        assert datalake.ingested[-1]["body"]["timestamp"] == 10_000 + 2 * WINDOW - 1  # the 20 left over from the first pass are the start of the second window


def test_an_anomalous_window_is_flagged_written_and_announced_over_mqtt():
    with running_fake_datalake() as (url, datalake), broker() as mqtt:
        values = _sine(50.0, 0, WINDOW) + _sine(50.0, 0, WINDOW) + [a + b for a, b in zip(_sine(50.0, 0, WINDOW), _sine(220.0, 0, WINDOW, 3.0))] + _sine(50.0, 0, WINDOW)
        datalake.points = _points(values, 20_000)
        config = _config(url, mqtt={"host": "127.0.0.1", "port": mqtt.port})
        follower = Follower(config, _registry_with_fitted_series(), DatalakeClient(url), sample_rate=RATE, publisher=MqttPublisher(config.mqtt), clock=lambda: 20.0)
        assert follower.tick() == 4
        flags = [i["body"]["fields"]["value_anomalous"] for i in datalake.ingested]
        assert flags == [0.0, 0.0, 1.0, 0.0]
        topics = [(t, retain) for t, _, retain in mqtt.messages]
        base = "hydra/anomaly/motor-1/vibration/value"
        # the first verdict says the state (retained), the alarm of the bad window, then the return to healthy changes the state again
        assert topics == [(f"{base}/state", True), (f"{base}/state", True), (f"{base}/alarm", False), (f"{base}/state", True)]
        alarm = json.loads(mqtt.messages[2][1])
        assert alarm["anomalous"] is True and alarm["score"] > alarm["threshold"] and alarm["sourceId"] == "motor-1"
        assert follower.snapshot()["anomalies"] == 1


def test_a_series_nobody_fitted_is_reported_and_scored_once_it_is():
    with running_fake_datalake() as (url, datalake):
        datalake.points = _points(_sine(50.0, 0, WINDOW), 30_000)
        registry = DetectorRegistry(_factory)
        messages: list[str] = []
        follower = Follower(_config(url), registry, DatalakeClient(url), sample_rate=RATE, clock=lambda: 30.0, log=messages.append)
        assert follower.tick() == 0 and follower.tick() == 0
        assert len(messages) == 1 and "no baseline" in messages[0]  # said once, not every pass
        assert follower.snapshot()["series"][0]["note"] == "not fitted" and datalake.ingested == []
        detector = registry.new_detector()
        detector.fit([_sine(50.0, 0) for _ in range(6)])
        registry.insert(KEY, detector)
        assert follower.tick() == 1


def test_a_hole_in_the_series_cuts_the_window_and_the_tail_before_it_is_dropped():
    with running_fake_datalake() as (url, datalake):
        first = _points(_sine(50.0, 0, WINDOW + 30), 40_000)
        second = _points(_sine(50.0, 0, WINDOW), 40_000 + WINDOW + 30 + 500)  # 500 ms of silence
        datalake.points = first + second
        follower = Follower(_config(url), _registry_with_fitted_series(), DatalakeClient(url), sample_rate=RATE, clock=lambda: 40.0)
        assert follower.tick() == 2  # one window before the hole, one after; the 30 samples before the hole never became a window
        stamps = [i["body"]["timestamp"] for i in datalake.ingested]
        assert stamps == [40_000 + WINDOW - 1, 40_000 + WINDOW + 30 + 500 + WINDOW - 1]


def test_a_datalake_that_is_down_is_counted_and_the_loop_goes_on():
    registry = _registry_with_fitted_series()
    messages: list[str] = []
    client = DatalakeClient("http://127.0.0.1:9", max_attempts=1, timeout_s=0.5)
    follower = Follower(_config("http://127.0.0.1:9"), registry, client, sample_rate=RATE, clock=lambda: 1.0, log=messages.append)
    assert follower.tick() == 0 and follower.tick() == 0
    assert follower.snapshot()["datalakeErrors"] == 2 and len(messages) == 1


def test_a_write_that_fails_does_not_lose_the_verdict_or_stop_the_follower():
    with running_fake_datalake() as (url, datalake):
        datalake.points = _points(_sine(50.0, 0, WINDOW * 2), 50_000)
        datalake.fail_ingest = True
        follower = Follower(_config(url), _registry_with_fitted_series(), DatalakeClient(url), sample_rate=RATE, clock=lambda: 50.0, log=lambda text: None)
        assert follower.tick() == 2
        snapshot = follower.snapshot()
        assert snapshot["writeErrors"] == 2 and snapshot["windowsScored"] == 2


def test_a_broker_that_refuses_is_counted_and_does_not_stop_the_scoring():
    with running_fake_datalake() as (url, datalake), broker(return_code=5) as mqtt:
        datalake.points = _points(_sine(50.0, 0, WINDOW), 60_000)
        config = _config(url, mqtt={"host": "127.0.0.1", "port": mqtt.port})
        follower = Follower(config, _registry_with_fitted_series(), DatalakeClient(url), sample_rate=RATE, publisher=MqttPublisher(config.mqtt), clock=lambda: 60.0, log=lambda text: None)
        assert follower.tick() == 1
        assert follower.snapshot()["mqttErrors"] == 1 and len(datalake.ingested) == 1


def test_verdicts_are_not_written_when_the_configuration_says_so():
    with running_fake_datalake() as (url, datalake):
        datalake.points = _points(_sine(50.0, 0, WINDOW), 70_000)
        follower = Follower(_config(url, writeScores=False), _registry_with_fitted_series(), DatalakeClient(url), sample_rate=RATE, clock=lambda: 70.0)
        assert follower.tick() == 1 and datalake.ingested == []


def test_the_loop_runs_and_stops_on_request():
    with running_fake_datalake() as (url, datalake):
        datalake.points = _points(_sine(50.0, 0, WINDOW), 80_000)
        follower = Follower(_config(url, intervalSeconds=0.1), _registry_with_fitted_series(), DatalakeClient(url), sample_rate=RATE, clock=lambda: 80.0)
        thread, stop = follower.start()
        deadline = threading.Event()
        for _ in range(50):
            if datalake.ingested:
                break
            deadline.wait(0.1)
        stop.set()
        thread.join(timeout=3)
        assert not thread.is_alive() and len(datalake.ingested) == 1


# ---------------------------------------------------------------------------------------------------------- tokens and the command
def test_the_client_sends_the_token_of_each_role():
    with running_fake_datalake() as (url, datalake):
        datalake.points = _points([1.0, 2.0], 90_000)
        client = DatalakeClient(url, read_token="r" * 20, ingest_token="i" * 20)
        client.query(source_id="motor-1", kind="vibration", field="value")
        client.ingest(source_id="motor-1", kind="anomaly_score", timestamp=1, fields={"value": 1.0})
        assert datalake.query_authorizations == ["Bearer " + "r" * 20]
        assert datalake.ingested[0]["authorization"] == "Bearer " + "i" * 20
        anonymous = DatalakeClient(url)
        anonymous.query(source_id="motor-1")
        assert datalake.query_authorizations[-1] is None


def test_the_command_refuses_a_follow_configuration_that_is_wrong(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.delenv("HYDRA_UMC_ANOMALY_TOKEN", raising=False)
    bad = tmp_path / "follow.json"
    bad.write_text(json.dumps({"datalake": {"url": "nonsense"}}), encoding="utf-8")
    assert main(["--addr", "127.0.0.1", "--port", "0", "--follow-config", str(bad)]) == 2
    assert "--follow-config" in capsys.readouterr().err
    assert main(["--addr", "127.0.0.1", "--port", "0", "--follow-config", str(tmp_path / "missing.json")]) == 2


def test_the_stats_route_shows_the_follower(tmp_path: Path):
    import urllib.request

    from hydra_umc_anomaly_detector.api import DetectorServer

    server = DetectorServer(("127.0.0.1", 0), AnomalyDetector(RATE, threshold=4.0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/stats"
        assert "follower" not in json.loads(urllib.request.urlopen(url, timeout=5).read())
        server.follower_snapshot = lambda: {"ticks": 7}
        assert json.loads(urllib.request.urlopen(url, timeout=5).read())["follower"] == {"ticks": 7}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
