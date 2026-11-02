# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - src/hydra_umc_anomaly_detector/fleet.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The fleet view and the maintenance proposal.

`FleetTracker` keeps, for every series the follower scores, its last anomaly score and how far the recent scores have drifted from a
reference (the mean score of healthy windows). `GET /fleet` shows them side by side. When a series stays drifted for long enough it can
PROPOSE a maintenance order to HYDRA-UMC-JOB-DISPATCHER; the proposal waits there for a person and nothing in this module stops, starts or
changes a machine. Everything is off unless the follow configuration turns it on (`fleet.enabled`, `maintenance.mode`).

The reference is the baseline mean of the drift monitor of the series when one was initialised (`/drift/init`), and otherwise the mean of
the first `referenceWindows` scores the follower saw (they are taken as healthy: say so in the documentation of the installation).
"""
from __future__ import annotations

import ipaddress
import json
import math
import threading
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from .registry import SeriesKey


class FleetConfigError(ValueError):
    """The fleet or maintenance section cannot be used; the message says which key and why."""


@dataclass(frozen=True)
class FleetConfig:
    enabled: bool = False
    drift_window: int = 20  # how many recent scores make the rolling mean
    reference_windows: int = 10  # how many first scores make the reference when no drift monitor gives one
    drift_ratio: float = 2.0  # rolling mean / reference at or above this is drift (the default of the drift monitor)


@dataclass(frozen=True)
class MaintenanceConfig:
    mode: str = "off"  # "off" or "propose"; nothing else exists on purpose: a drift never executes anything
    url: str = ""
    token: str | None = None
    sustained_windows: int = 12  # consecutive drifted evaluations
    sustained_seconds: float = 600.0  # and at least this long between the first and the last (timestamps of the samples)
    resend_seconds: float = 21600.0  # while the drift lasts, tell the dispatcher again after this long (it merges duplicates)
    timeout_seconds: float = 5.0


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _num(raw: dict, key: str, default: float, low: float, high: float, integer: bool = False) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (integer and not isinstance(value, int)):
        raise FleetConfigError(f"{key} must be a {'whole ' if integer else ''}number")
    if not low <= value <= high:
        raise FleetConfigError(f"{key} must be between {low:g} and {high:g}")
    return value


def parse_fleet_config(raw: object) -> FleetConfig:
    if raw is None:
        return FleetConfig()
    if not isinstance(raw, dict):
        raise FleetConfigError("fleet must be an object")
    unknown = set(raw) - {"enabled", "driftWindow", "referenceWindows", "driftRatio"}
    if unknown:
        raise FleetConfigError(f"fleet: unknown key {sorted(unknown)[0]}")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise FleetConfigError("fleet.enabled must be true or false")
    return FleetConfig(
        enabled=enabled,
        drift_window=int(_num(raw, "driftWindow", 20, 2, 10000, integer=True)),
        reference_windows=int(_num(raw, "referenceWindows", 10, 2, 10000, integer=True)),
        drift_ratio=float(_num(raw, "driftRatio", 2.0, 1.01, 1000.0)),
    )


def parse_maintenance_config(raw: object, *, read_secret: Callable[[object, str], str | None]) -> MaintenanceConfig:
    if raw is None:
        return MaintenanceConfig()
    if not isinstance(raw, dict):
        raise FleetConfigError("maintenance must be an object")
    unknown = set(raw) - {"mode", "url", "tokenFile", "allowPlaintext", "sustainedWindows", "sustainedSeconds", "resendSeconds", "timeoutSeconds"}
    if unknown:
        raise FleetConfigError(f"maintenance: unknown key {sorted(unknown)[0]}")
    mode = raw.get("mode", "off")
    if mode not in ("off", "propose"):
        raise FleetConfigError('maintenance.mode must be "off" or "propose" (a drift never creates or runs an order by itself)')
    url = raw.get("url", "")
    token = read_secret(raw.get("tokenFile"), "maintenance.tokenFile")
    if mode == "propose":
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise FleetConfigError("maintenance.url must be the http:// or https:// address of HYDRA-UMC-JOB-DISPATCHER")
        parsed = urlparse(url)
        if parsed.username or parsed.password or not parsed.hostname:
            raise FleetConfigError("maintenance.url must not carry credentials")
        if parsed.scheme == "http" and not _is_loopback(parsed.hostname) and not raw.get("allowPlaintext", False):
            raise FleetConfigError("maintenance.url is not on this machine: use https, or set maintenance.allowPlaintext to say on purpose that plain text is fine")
    return MaintenanceConfig(
        mode=mode,
        url=url.rstrip("/") if isinstance(url, str) else "",
        token=token,
        sustained_windows=int(_num(raw, "sustainedWindows", 12, 1, 100000, integer=True)),
        sustained_seconds=float(_num(raw, "sustainedSeconds", 600.0, 0.0, 31536000.0)),
        resend_seconds=float(_num(raw, "resendSeconds", 21600.0, 60.0, 31536000.0)),
        timeout_seconds=float(_num(raw, "timeoutSeconds", 5.0, 0.5, 60.0)),
    )


@dataclass
class _Track:
    recent: deque = field(default_factory=deque)
    reference_scores: list = field(default_factory=list)
    last_score: float | None = None
    last_timestamp: int | None = None
    anomalous: bool | None = None
    threshold: float | None = None
    windows: int = 0
    drift_streak: int = 0
    drift_since: int | None = None  # timestamp (ms) of the first drifted evaluation of the current streak
    proposed_at: float | None = None  # clock() of the last proposal sent
    proposals_sent: int = 0
    last_proposal_note: str = ""


class MaintenanceProposer:
    """Sends the proposal to HYDRA-UMC-JOB-DISPATCHER. It only ever POSTs to /maintenance/proposals."""

    def __init__(self, config: MaintenanceConfig) -> None:
        self.config = config

    def propose(self, source: str, reason: str, score: float, drift: float) -> str:
        body = json.dumps({"source": source, "reason": reason, "score": score, "drift": drift}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.config.token:
            headers["Authorization"] = f"Bearer {self.config.token}"
        request = urllib.request.Request(f"{self.config.url}/maintenance/proposals", data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                response.read()
                return f"accepted (HTTP {response.status})"
        except urllib.error.HTTPError as error:
            raise OSError(f"the dispatcher answered HTTP {error.code}") from error
        except urllib.error.URLError as error:
            raise OSError(f"could not reach the dispatcher: {error.reason}") from error


class FleetTracker:
    def __init__(
        self,
        fleet: FleetConfig,
        maintenance: MaintenanceConfig,
        *,
        reference_for: Callable[[SeriesKey], float | None] | None = None,
        proposer: MaintenanceProposer | None = None,
        clock: Callable[[], float] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        import time

        self.fleet = fleet
        self.maintenance = maintenance
        self._reference_for = reference_for or (lambda key: None)
        self._proposer = proposer if proposer is not None else (MaintenanceProposer(maintenance) if maintenance.mode == "propose" else None)
        self._clock = clock or time.time
        self._log = log or (lambda text: None)
        self._tracks: dict[SeriesKey, _Track] = {}
        self._lock = threading.Lock()

    def _reference(self, key: SeriesKey, track: _Track) -> float | None:
        external = self._reference_for(key)
        if external is not None and math.isfinite(external) and external > 0:
            return external
        if len(track.reference_scores) < self.fleet.reference_windows:
            return None
        mean = sum(track.reference_scores) / len(track.reference_scores)
        return mean if mean > 0 else None

    def observe(self, key: SeriesKey, score: float, timestamp: int, anomalous: bool, threshold: float | None) -> None:
        """Called by the follower for every window it scores."""
        if not self.fleet.enabled or not math.isfinite(score):
            return
        to_propose: tuple[float, float] | None = None
        with self._lock:
            track = self._tracks.setdefault(key, _Track(recent=deque(maxlen=self.fleet.drift_window)))
            track.windows += 1
            track.last_score, track.last_timestamp, track.anomalous, track.threshold = score, timestamp, anomalous, threshold
            if len(track.reference_scores) < self.fleet.reference_windows and self._reference_for(key) is None:
                track.reference_scores.append(score)  # the first windows are taken as healthy: they are the reference, not drift evidence
            else:
                track.recent.append(score)
            drifted, ratio = self._drift(key, track)
            if drifted:
                track.drift_streak += 1
                if track.drift_since is None:
                    track.drift_since = timestamp
            else:
                track.drift_streak, track.drift_since = 0, None
                track.proposed_at = None
            if drifted and self._sustained(track, timestamp) and self._proposer is not None:
                now = self._clock()
                if track.proposed_at is None or now - track.proposed_at >= self.maintenance.resend_seconds:
                    track.proposed_at = now
                    to_propose = (score, ratio if ratio is not None else 0.0)
        if to_propose is not None:
            self._send(key, *to_propose)

    def _drift(self, key: SeriesKey, track: _Track) -> tuple[bool, float | None]:
        reference = self._reference(key, track)
        if reference is None or len(track.recent) < self.fleet.drift_window:
            return False, None
        ratio = (sum(track.recent) / len(track.recent)) / reference
        return ratio >= self.fleet.drift_ratio, ratio

    def _sustained(self, track: _Track, timestamp: int) -> bool:
        if track.drift_streak < self.maintenance.sustained_windows or track.drift_since is None:
            return False
        return (timestamp - track.drift_since) / 1000.0 >= self.maintenance.sustained_seconds

    def _send(self, key: SeriesKey, score: float, ratio: float) -> None:
        source = f"{key.source_id}/{key.kind}/{key.field}"
        safe = f"{key.source_id}:{key.kind}:{key.field}"
        reason = f"Sustained drift of the anomaly score: the recent mean is {ratio:.2f} times the healthy reference ({source})."
        try:
            note = self._proposer.propose(safe[:128], reason, score, ratio)  # type: ignore[union-attr]
        except OSError as error:
            note = f"not delivered: {error}"
            self._log(f"maintenance proposal for {source} {note}")
            with self._lock:
                track = self._tracks[key]
                track.proposed_at = None  # try again with the next window
                track.last_proposal_note = note
            return
        with self._lock:
            track = self._tracks[key]
            track.proposals_sent += 1
            track.last_proposal_note = note

    def snapshot(self, listed: list[SeriesKey]) -> dict[str, object]:
        """The fleet view: one row per series the follower is told about (a series with no window yet has no score)."""
        rows = []
        with self._lock:
            for key in listed:
                track = self._tracks.get(key)
                if track is None:
                    rows.append({"sourceId": key.source_id, "kind": key.kind, "field": key.field, "windowsScored": 0, "lastScore": None, "lastTimestamp": None,
                                 "anomalous": None, "threshold": None, "driftRatio": None, "drifted": False, "driftSustained": False, "driftSince": None})
                    continue
                drifted, ratio = self._drift(key, track)
                rows.append({
                    "sourceId": key.source_id, "kind": key.kind, "field": key.field, "windowsScored": track.windows,
                    "lastScore": track.last_score, "lastTimestamp": track.last_timestamp, "anomalous": track.anomalous, "threshold": track.threshold,
                    "driftRatio": ratio, "drifted": drifted,
                    "driftSustained": bool(drifted and self._sustained(track, track.last_timestamp or 0)),
                    "driftSince": track.drift_since,
                    "proposalsSent": track.proposals_sent, "proposalNote": track.last_proposal_note or None,
                })
        return {"driftRatioLimit": self.fleet.drift_ratio, "driftWindow": self.fleet.drift_window, "maintenanceMode": self.maintenance.mode, "series": rows}
