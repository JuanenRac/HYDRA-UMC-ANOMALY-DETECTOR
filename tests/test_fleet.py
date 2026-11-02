# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - tests/test_fleet.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The fleet view (GET /fleet) and the maintenance proposal: off by default, drift measured against a healthy reference, a proposal only
after a sustained drift, once per episode, to a stand-in of the dispatcher that records what it receives."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hydra_umc_anomaly_detector.api import DetectorServer
from hydra_umc_anomaly_detector.detector import AnomalyDetector
from hydra_umc_anomaly_detector.fleet import FleetConfig, FleetTracker, MaintenanceConfig
from hydra_umc_anomaly_detector.follower import FollowConfigError, parse_follow_config
from hydra_umc_anomaly_detector.registry import SeriesKey

KEY = SeriesKey("motor-1", "vibration", "value")
OTHER = SeriesKey("motor-2", "vibration", "value")


class FakeDispatcher:
    def __init__(self, status: int = 201) -> None:
        self.received: list[dict] = []
        self.status = status
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: D401
                pass

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                owner.received.append({"path": self.path, "body": body, "auth": self.headers.get("Authorization")})
                payload = b"{}"
                self.send_response(owner.status)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@contextmanager
def dispatcher(status: int = 201):
    d = FakeDispatcher(status)
    try:
        yield d
    finally:
        d.close()


def _tracker(url: str = "", mode: str = "propose", **over):
    fleet = FleetConfig(enabled=True, drift_window=4, reference_windows=3, drift_ratio=2.0)
    maintenance = MaintenanceConfig(mode=mode, url=url, token=over.pop("token", None), sustained_windows=over.pop("sustained_windows", 3),
                                    sustained_seconds=over.pop("sustained_seconds", 2.0), resend_seconds=over.pop("resend_seconds", 3600.0), timeout_seconds=2.0)
    clock = over.pop("clock", None)
    return FleetTracker(fleet, maintenance, clock=clock, **over)


def _feed(tracker: FleetTracker, key: SeriesKey, scores: list[float], start_ms: int = 0, step_ms: int = 1000) -> int:
    t = start_ms
    for score in scores:
        tracker.observe(key, score, t, False, 4.0)
        t += step_ms
    return t


def _row(tracker: FleetTracker, key: SeriesKey = KEY) -> dict:
    return next(r for r in tracker.snapshot([key])["series"])


# ---------------------------------------------------------------------------------------------------------- off by default
def test_everything_is_off_without_the_section():
    config = parse_follow_config({"datalake": {"url": "http://127.0.0.1:8095"}, "series": [{"sourceId": "a", "kind": "k", "field": "f", "windowSize": 64}]})
    assert config.fleet.enabled is False and config.maintenance.mode == "off"


def test_a_disabled_tracker_records_nothing():
    tracker = FleetTracker(FleetConfig(), MaintenanceConfig())
    tracker.observe(KEY, 5.0, 1000, True, 4.0)
    assert _row(tracker)["lastScore"] is None


@pytest.mark.parametrize("change", [
    {"fleet": {"enabled": "yes"}},
    {"fleet": {"driftRatio": 1.0}},
    {"fleet": {"driftWindow": 1}},
    {"fleet": {"unknown": 1}},
    {"maintenance": {"mode": "create"}},
    {"maintenance": {"mode": "off", "extra": 1}},
    {"fleet": {"enabled": True}, "maintenance": {"mode": "propose"}},
    {"fleet": {"enabled": True}, "maintenance": {"mode": "propose", "url": "http://dispatcher.example.com:8090"}},
    {"fleet": {"enabled": True}, "maintenance": {"mode": "propose", "url": "http://user:pw@127.0.0.1:8090"}},
    {"fleet": {"enabled": True}, "maintenance": {"mode": "propose", "url": "http://127.0.0.1:8090", "sustainedWindows": 0}},
])
def test_a_bad_fleet_or_maintenance_section_is_refused(change):
    raw = {"datalake": {"url": "http://127.0.0.1:8095"}, "series": [{"sourceId": "a", "kind": "k", "field": "f", "windowSize": 64}]}
    raw.update(change)
    with pytest.raises(FollowConfigError):
        parse_follow_config(raw)


def test_a_dispatcher_off_this_machine_needs_https_or_a_stated_choice():
    base = {"datalake": {"url": "http://127.0.0.1:8095"}, "series": [{"sourceId": "a", "kind": "k", "field": "f", "windowSize": 64}], "fleet": {"enabled": True}}
    assert parse_follow_config({**base, "maintenance": {"mode": "propose", "url": "https://dispatcher.example.com"}}).maintenance.mode == "propose"
    assert parse_follow_config({**base, "maintenance": {"mode": "propose", "url": "http://dispatcher.example.com", "allowPlaintext": True}}).maintenance.mode == "propose"


# ---------------------------------------------------------------------------------------------------------- the fleet view
def test_the_view_lists_every_followed_series_with_its_last_score_and_drift():
    tracker = _tracker(mode="off")
    _feed(tracker, KEY, [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 5.0])
    view = tracker.snapshot([KEY, OTHER])
    rows = {r["sourceId"]: r for r in view["series"]}
    assert rows["motor-1"]["lastScore"] == 5.0 and rows["motor-1"]["windowsScored"] == 7
    assert rows["motor-1"]["driftRatio"] == pytest.approx((1.0 + 1.0 + 1.0 + 5.0) / 4)  # the four newest over the reference of 1.0
    assert rows["motor-1"]["drifted"] is True and rows["motor-1"]["driftSustained"] is False
    assert rows["motor-2"]["lastScore"] is None and rows["motor-2"]["drifted"] is False


def test_no_drift_is_reported_before_the_reference_and_the_window_are_complete():
    tracker = _tracker(mode="off")
    _feed(tracker, KEY, [1.0, 1.0, 9.0, 9.0])  # the first three are the reference; only one score is recent
    assert _row(tracker)["driftRatio"] is None and _row(tracker)["drifted"] is False


def test_the_reference_comes_from_the_drift_monitor_when_there_is_one():
    tracker = _tracker(mode="off", reference_for=lambda key: 2.0)
    _feed(tracker, KEY, [4.0, 4.0, 4.0, 4.0])
    assert _row(tracker)["driftRatio"] == pytest.approx(2.0) and _row(tracker)["drifted"] is True


# ---------------------------------------------------------------------------------------------------------- the proposal
def test_a_sustained_drift_proposes_once_and_only_proposes():
    with dispatcher() as d:
        tracker = _tracker(d.url, token="secret-token")
        end = _feed(tracker, KEY, [1.0, 1.0, 1.0])  # reference
        _feed(tracker, KEY, [5.0] * 10, start_ms=end)
        assert len(d.received) == 1
        sent = d.received[0]
        assert sent["path"] == "/maintenance/proposals" and sent["auth"] == "Bearer secret-token"
        assert sent["body"]["source"] == "motor-1:vibration:value" and sent["body"]["drift"] >= 2.0 and "Sustained drift" in sent["body"]["reason"]
        assert set(sent["body"]) == {"source", "reason", "score", "drift"}
        assert _row(tracker)["driftSustained"] is True and _row(tracker)["proposalsSent"] == 1


def test_a_short_blip_proposes_nothing():
    with dispatcher() as d:
        tracker = _tracker(d.url, sustained_windows=6)
        end = _feed(tracker, KEY, [1.0, 1.0, 1.0, 1.0, 1.0, 1.0])
        end = _feed(tracker, KEY, [9.0, 9.0], start_ms=end)  # the rolling mean stays above the limit for 5 evaluations, fewer than the 6 the rule asks
        _feed(tracker, KEY, [1.0] * 6, start_ms=end)
        assert d.received == []


def test_the_drift_must_last_in_time_too():
    with dispatcher() as d:
        tracker = _tracker(d.url, sustained_windows=2, sustained_seconds=100.0)
        end = _feed(tracker, KEY, [1.0, 1.0, 1.0])
        _feed(tracker, KEY, [9.0] * 8, start_ms=end, step_ms=1000)  # 8 s is below 100 s
        assert d.received == []


def test_mode_off_measures_the_drift_but_sends_nothing():
    with dispatcher() as d:
        tracker = _tracker(d.url, mode="off")
        end = _feed(tracker, KEY, [1.0, 1.0, 1.0])
        _feed(tracker, KEY, [9.0] * 12, start_ms=end)
        assert d.received == [] and _row(tracker)["driftSustained"] is True


def test_the_proposal_is_repeated_only_after_the_resend_time():
    now = [1000.0]
    with dispatcher() as d:
        tracker = _tracker(d.url, resend_seconds=3600.0, clock=lambda: now[0])
        end = _feed(tracker, KEY, [1.0, 1.0, 1.0])
        end = _feed(tracker, KEY, [9.0] * 8, start_ms=end)
        assert len(d.received) == 1
        now[0] += 7200.0
        _feed(tracker, KEY, [9.0], start_ms=end)
        assert len(d.received) == 2


def test_an_unreachable_dispatcher_is_reported_and_tried_again():
    tracker = _tracker("http://127.0.0.1:9")  # nothing listens on the discard port
    end = _feed(tracker, KEY, [1.0, 1.0, 1.0])
    _feed(tracker, KEY, [9.0] * 8, start_ms=end)
    row = _row(tracker)
    assert row["proposalsSent"] == 0 and "not delivered" in row["proposalNote"]


def test_a_refusal_of_the_dispatcher_is_reported():
    with dispatcher(status=404) as d:  # the dispatcher has the proposals switched off
        tracker = _tracker(d.url)
        end = _feed(tracker, KEY, [1.0, 1.0, 1.0])
        _feed(tracker, KEY, [9.0] * 8, start_ms=end)
        assert _row(tracker)["proposalsSent"] == 0 and "404" in _row(tracker)["proposalNote"]


# ---------------------------------------------------------------------------------------------------------- GET /fleet
@contextmanager
def _server(snapshot=None, token=None):
    server = DetectorServer(("127.0.0.1", 0), AnomalyDetector(sample_rate=1000.0, threshold=4.0), token=token)
    server.fleet_snapshot = snapshot
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _get(url: str, token: str | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_get_fleet_is_an_unknown_route_while_the_view_is_off():
    with _server() as url:
        assert _get(f"{url}/fleet")[0] == 404


def test_get_fleet_answers_the_view_and_follows_the_read_protection():
    tracker = _tracker(mode="off")
    _feed(tracker, KEY, [1.0, 2.0])
    with _server(lambda: tracker.snapshot([KEY]), token="a-long-enough-token") as url:
        assert _get(f"{url}/fleet")[0] == 401
        status, body = _get(f"{url}/fleet", "a-long-enough-token")
        assert status == 200 and body["series"][0]["lastScore"] == 2.0 and body["maintenanceMode"] == "off"
