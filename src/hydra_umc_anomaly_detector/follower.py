# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - src/hydra_umc_anomaly_detector/follower.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Continuous service mode: read each configured series from DATALAKE as it grows, score it, write the verdicts back and raise alarms.

Until now a verdict existed only when somebody posted a window to `/detect`. The follower closes the loop for the series it is told
about (`--follow-config FILE`, everything tuned lives in that file):

  * every `intervalSeconds` it asks DATALAKE for the points of a series after the last one it used, cuts them wherever the sampling is
    not regular (a window made over a hole describes the hole), and scores every complete window with the detector of that series
    (the per-series detectors of `registry.py`: a series that was never fitted is reported, not guessed);
  * each verdict is written back as a sample of kind `scoreKind` (default `anomaly_score`) with the fields `<field>` (the score),
    `<field>_anomalous` (0 or 1) and `<field>_model` (the model version), timestamped with the last sample of the window;
  * a series that changes state (healthy -> anomalous or back) publishes a retained message to `<topicPrefix>/<sourceId>/<kind>/<field>/state`,
    and every anomalous window publishes `.../alarm`, over MQTT, when `mqtt` is configured.

A DATALAKE or broker that cannot be reached never stops the loop: the failure is counted, shown in `GET /stats` and tried again next time.
"""
from __future__ import annotations

import ipaddress
import json
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .datalake_client import DatalakeClient, DatalakeError, split_runs
from .mqtt_publish import MqttError, MqttPublisher, MqttTarget
from .registry import DetectorRegistry, RegistryError, SeriesKey, parse_key

DEFAULT_INTERVAL_S = 5.0
DEFAULT_MAX_WINDOWS_PER_TICK = 20
DEFAULT_TOPIC_PREFIX = "hydra/anomaly"


class FollowConfigError(ValueError):
    """The follow configuration cannot be used; the message says which key and why."""


@dataclass(frozen=True)
class SeriesConfig:
    key: SeriesKey
    window_size: int


@dataclass(frozen=True)
class FollowConfig:
    datalake_url: str
    series: tuple[SeriesConfig, ...]
    interval_s: float = DEFAULT_INTERVAL_S
    max_windows_per_tick: int = DEFAULT_MAX_WINDOWS_PER_TICK
    write_scores: bool = True
    score_kind: str = "anomaly_score"
    lookback_windows: int = 2
    gap_tolerance: float = 0.5
    mqtt: MqttTarget | None = None
    topic_prefix: str = DEFAULT_TOPIC_PREFIX


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _read_secret(path: object, label: str) -> str | None:
    if path is None:
        return None
    if not isinstance(path, str) or not path:
        raise FollowConfigError(f"{label} must be the path of a file")
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as error:
        raise FollowConfigError(f"cannot read {label}: {error}") from error


def _number(raw: dict, key: str, default: float, minimum: float, maximum: float, integer: bool = False) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (integer and not isinstance(value, int)):
        raise FollowConfigError(f"{key} must be a {'whole ' if integer else ''}number")
    if not minimum <= value <= maximum:
        raise FollowConfigError(f"{key} must be between {minimum:g} and {maximum:g}")
    return value


def parse_follow_config(raw: object) -> FollowConfig:
    if not isinstance(raw, dict):
        raise FollowConfigError("the configuration must be a JSON object")
    datalake = raw.get("datalake")
    if not isinstance(datalake, dict) or not isinstance(datalake.get("url"), str) or not datalake["url"].startswith(("http://", "https://")):
        raise FollowConfigError("datalake.url must be an http:// or https:// address")
    series_raw = raw.get("series")
    if not isinstance(series_raw, list) or not series_raw:
        raise FollowConfigError("series must be a list with at least one series")
    series: list[SeriesConfig] = []
    for index, item in enumerate(series_raw):
        if not isinstance(item, dict):
            raise FollowConfigError(f"series[{index}] must be an object")
        try:
            key = parse_key(item.get("sourceId"), item.get("kind"), item.get("field"))  # type: ignore[arg-type]
        except RegistryError as error:
            raise FollowConfigError(f"series[{index}]: {error}") from error
        window_size = item.get("windowSize")
        if isinstance(window_size, bool) or not isinstance(window_size, int) or not 8 <= window_size <= 65536:
            raise FollowConfigError(f"series[{index}].windowSize must be a whole number from 8 to 65536")
        if any(existing.key == key for existing in series):
            raise FollowConfigError(f"series[{index}] repeats {key.source_id}/{key.kind}/{key.field}")
        series.append(SeriesConfig(key, window_size))
    score_kind = raw.get("scoreKind", "anomaly_score")
    try:
        score_kind = parse_key("x", score_kind, "x").kind
    except RegistryError as error:
        raise FollowConfigError(f"scoreKind: {error}") from error
    mqtt_target: MqttTarget | None = None
    topic_prefix = raw.get("topicPrefix", DEFAULT_TOPIC_PREFIX)
    if not isinstance(topic_prefix, str) or not topic_prefix or any(c in topic_prefix for c in "#+\x00") or topic_prefix.endswith("/"):
        raise FollowConfigError("topicPrefix must be a topic without wildcards and without a trailing slash")
    mqtt_raw = raw.get("mqtt")
    if mqtt_raw is not None:
        if not isinstance(mqtt_raw, dict) or not isinstance(mqtt_raw.get("host"), str) or not mqtt_raw["host"]:
            raise FollowConfigError("mqtt.host must be the address of the broker")
        host = mqtt_raw["host"]
        tls = bool(mqtt_raw.get("tls", False))
        allow_plaintext = bool(mqtt_raw.get("allowPlaintext", False))
        if not tls and not _is_loopback(host) and not allow_plaintext:
            raise FollowConfigError("mqtt.host is not on this machine: set mqtt.tls, or mqtt.allowPlaintext to say on purpose that plain text is fine")
        client_id = mqtt_raw.get("clientId", "hydra-umc-anomaly-detector")
        if not isinstance(client_id, str) or not 1 <= len(client_id) <= 64:
            raise FollowConfigError("mqtt.clientId must be 1 to 64 characters")
        mqtt_target = MqttTarget(
            host=host,
            port=int(_number(mqtt_raw, "port", 8883 if tls else 1883, 1, 65535, integer=True)),
            client_id=client_id,
            username=_read_secret(mqtt_raw.get("usernameFile"), "mqtt.usernameFile"),
            password=_read_secret(mqtt_raw.get("passwordFile"), "mqtt.passwordFile"),
            tls=tls,
            ca_file=mqtt_raw.get("caFile") if isinstance(mqtt_raw.get("caFile"), str) else None,
            timeout_s=float(_number(mqtt_raw, "timeoutSeconds", 5.0, 0.5, 60.0)),
        )
    return FollowConfig(
        datalake_url=datalake["url"],
        series=tuple(series),
        interval_s=float(_number(raw, "intervalSeconds", DEFAULT_INTERVAL_S, 0.1, 3600.0)),
        max_windows_per_tick=int(_number(raw, "maxWindowsPerTick", DEFAULT_MAX_WINDOWS_PER_TICK, 1, 1000, integer=True)),
        write_scores=bool(raw.get("writeScores", True)),
        score_kind=score_kind,
        lookback_windows=int(_number(raw, "lookbackWindows", 2, 1, 100, integer=True)),
        gap_tolerance=float(_number(raw, "gapTolerance", 0.5, 0.0, 0.99)),
        mqtt=mqtt_target,
        topic_prefix=topic_prefix,
    )


def load_follow_config(path: str) -> tuple[FollowConfig, dict]:
    """The parsed configuration and the raw object (the token files named in it are read by the caller)."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FollowConfigError(f"cannot read {path}: {error}") from error
    return parse_follow_config(raw), raw


@dataclass
class SeriesState:
    cursor: int | None = None  # timestamp (ms) of the last sample already used
    anomalous: bool | None = None
    last_score: float | None = None
    last_timestamp: int | None = None
    windows_scored: int = 0
    note: str = "waiting"


@dataclass
class FollowerStats:
    ticks: int = 0
    windows_scored: int = 0
    anomalies: int = 0
    datalake_errors: int = 0
    write_errors: int = 0
    mqtt_errors: int = 0
    not_fitted: int = 0


class Follower:
    def __init__(
        self,
        config: FollowConfig,
        registry: DetectorRegistry,
        client: DatalakeClient,
        *,
        sample_rate: float,
        publisher: MqttPublisher | None = None,
        clock=time.time,
        log=None,
    ) -> None:
        self.config = config
        self.registry = registry
        self.client = client
        self.sample_rate = sample_rate
        self.publisher = publisher
        self._clock = clock
        self._log = log or (lambda text: print(f"[anomaly-detector] {text}", file=sys.stderr))
        self.state: dict[SeriesKey, SeriesState] = {s.key: SeriesState() for s in config.series}
        self.stats = FollowerStats()
        self._lock = threading.Lock()
        self._warned: set[tuple[SeriesKey, str]] = set()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "ticks": self.stats.ticks, "windowsScored": self.stats.windows_scored, "anomalies": self.stats.anomalies,
                "datalakeErrors": self.stats.datalake_errors, "writeErrors": self.stats.write_errors, "mqttErrors": self.stats.mqtt_errors,
                "notFitted": self.stats.not_fitted,
                "series": [
                    {
                        "sourceId": key.source_id, "kind": key.kind, "field": key.field, "note": st.note,
                        "anomalous": st.anomalous, "lastScore": st.last_score, "lastTimestamp": st.last_timestamp, "windowsScored": st.windows_scored,
                    }
                    for key, st in self.state.items()
                ],
            }

    def _warn_once(self, key: SeriesKey, what: str, text: str) -> None:
        if (key, what) not in self._warned:
            self._warned.add((key, what))
            self._log(text)

    def tick(self) -> int:
        """One pass over every series. Returns the number of windows scored."""
        scored = 0
        for series in self.config.series:
            scored += self._follow(series)
        with self._lock:
            self.stats.ticks += 1
        return scored

    def _follow(self, series: SeriesConfig) -> int:
        key, st = series.key, self.state[series.key]
        entry = self.registry.get(key)
        if entry is None or not entry.detector.is_fitted:
            with self._lock:
                st.note = "not fitted"
                self.stats.not_fitted += 1
            self._warn_once(key, "fit", f"{key.source_id}/{key.kind}/{key.field} has no baseline yet: fit it (POST /detectors/.../fit) and it will be scored")
            return 0
        self._warned.discard((key, "fit"))
        period_ms = 1000.0 / self.sample_rate
        start = st.cursor + 1 if st.cursor is not None else int(self._clock() * 1000 - series.window_size * period_ms * self.config.lookback_windows)
        limit = series.window_size * self.config.max_windows_per_tick + series.window_size
        try:
            points = self.client.query(source_id=key.source_id, kind=key.kind, field=key.field, start=start, limit=limit)
        except DatalakeError as error:
            with self._lock:
                st.note = "DATALAKE unreachable"
                self.stats.datalake_errors += 1
            self._warn_once(key, "datalake", f"cannot read {key.source_id}/{key.kind}/{key.field}: {error}")
            return 0
        self._warned.discard((key, "datalake"))
        if not points:
            with self._lock:
                st.note = "no new samples"
            return 0
        runs = split_runs(points, self.sample_rate, self.config.gap_tolerance)
        scored = 0
        for run_index, run in enumerate(runs):
            last_run = run_index == len(runs) - 1
            count = len(run) // series.window_size
            for window_index in range(count):
                if scored >= self.config.max_windows_per_tick:
                    break
                chunk = run[window_index * series.window_size : (window_index + 1) * series.window_size]
                self._score(series, entry, chunk[-1].timestamp, [p.value for p in chunk])
                scored += 1
                st.cursor = chunk[-1].timestamp
            if not last_run and scored < self.config.max_windows_per_tick and run:
                st.cursor = run[-1].timestamp  # a tail that a hole cut off can never become a window
        with self._lock:
            st.note = "following" if scored else "waiting for a whole window"
        return scored

    def _score(self, series: SeriesConfig, entry, timestamp: int, values: list[float]) -> None:
        key, st = series.key, self.state[series.key]
        try:
            with entry.lock:
                verdict = entry.detector.score(values)
        except Exception as error:  # a window the detector refuses (wrong length, not finite): reported, never fatal
            with self._lock:
                st.note = f"window refused: {error}"
            self._warn_once(key, "window", f"{key.source_id}/{key.kind}/{key.field}: a window was refused: {error}")
            return
        with self._lock:
            self.stats.windows_scored += 1
            st.windows_scored += 1
            st.last_score, st.last_timestamp = verdict.score, timestamp
            if verdict.anomalous:
                self.stats.anomalies += 1
            changed = st.anomalous is not None and st.anomalous != verdict.anomalous
            first = st.anomalous is None
            st.anomalous = verdict.anomalous
        if self.config.write_scores:
            fields = {key.field: verdict.score, f"{key.field}_anomalous": 1.0 if verdict.anomalous else 0.0, f"{key.field}_model": float(verdict.model_version)}
            try:
                self.client.ingest(source_id=key.source_id, kind=self.config.score_kind, timestamp=timestamp, fields=fields)
            except DatalakeError as error:
                with self._lock:
                    self.stats.write_errors += 1
                self._warn_once(key, "write", f"cannot write the verdict of {key.source_id}/{key.kind}/{key.field}: {error}")
            else:
                self._warned.discard((key, "write"))
        if self.publisher is not None:
            message = json.dumps({
                "sourceId": key.source_id, "kind": key.kind, "field": key.field, "timestamp": timestamp, "score": verdict.score,
                "threshold": verdict.threshold, "anomalous": verdict.anomalous, "modelVersion": verdict.model_version,
            }).encode("utf-8")
            base = f"{self.config.topic_prefix}/{key.source_id}/{key.kind}/{key.field}"
            try:
                if changed or first:
                    self.publisher.publish(f"{base}/state", message, retain=True)
                if verdict.anomalous:
                    self.publisher.publish(f"{base}/alarm", message, retain=False)
            except MqttError as error:
                with self._lock:
                    self.stats.mqtt_errors += 1
                self._warn_once(key, "mqtt", f"cannot publish the alarm of {key.source_id}/{key.kind}/{key.field}: {error}")
            else:
                self._warned.discard((key, "mqtt"))

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception as error:  # the loop of a service does not die of one bad pass
                self._log(f"follower pass failed: {error}")
            stop.wait(self.config.interval_s)

    def start(self) -> tuple[threading.Thread, threading.Event]:
        stop = threading.Event()
        thread = threading.Thread(target=self.run, args=(stop,), name="anomaly-follower", daemon=True)
        thread.start()
        return thread, stop
