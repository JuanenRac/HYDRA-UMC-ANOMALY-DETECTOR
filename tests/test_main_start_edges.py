# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - tests/test_main_start_edges.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The rest of the start of the service (test_main_start.py has the first half): the options that stop it with exit 2 (token, address, identity key,
training range, the data lake options that go together, the settings of the diagnosis and the order tracking), a fit from the data lake that works
(and one cut short, and one whose baseline cannot be saved), the series restored from a folder, and the continuous follow mode with its
alarms and fleet view. The server, the follower and the publisher are stand-ins: nothing listens and nothing is sent."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from hydra_umc_anomaly_detector import main as detector_main
from hydra_umc_anomaly_detector.follower import FollowConfigError


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in ("HYDRA_UMC_DATALAKE_READ_TOKEN", "HYDRA_UMC_DATALAKE_INGEST_TOKEN", "HYDRA_UMC_DATALAKE_TOKEN", "HYDRA_UMC_ANOMALY_PROTECT_READS",
                 "HYDRA_UMC_ANOMALY_TOKEN", "HYDRA_UMC_ANOMALY_DETECTOR_IDENTITY_KEY_FILE"):
        monkeypatch.delenv(name, raising=False)


class Server:
    made: list = []

    def __init__(self, address, detector, baseline_path, **options) -> None:
        self.address, self.detector, self.baseline_path, self.options = address, detector, baseline_path, options
        self.closed = False
        Server.made.append(self)

    def serve_forever(self) -> None:
        raise KeyboardInterrupt

    def server_close(self) -> None:
        self.closed = True


@pytest.fixture
def server(monkeypatch):
    Server.made = []
    monkeypatch.setattr(detector_main, "DetectorServer", Server)
    return Server.made


def start(*extra: str) -> int:
    return detector_main.main(["--port", "0", *extra])


class TestOptionsThatStopTheStart:
    def test_a_diagnosis_or_order_tracking_block_that_cannot_hold_is_exit_2(self, tmp_path: Path, capsys, server) -> None:
        for key in ("diagnosis", "orderTracking"):
            config = tmp_path / f"{key}.json"
            config.write_text('{"%s": 5}' % key, encoding="utf-8")
            assert start("--detector-config", str(config)) == 2
            assert "--detector-config" in capsys.readouterr().err
        assert server == []

    def test_a_window_the_fft_does_not_know_or_too_few_windows_is_exit_2(self, tmp_path: Path, capsys, server) -> None:
        config = tmp_path / "window.json"
        config.write_text('{"window": "triangle"}', encoding="utf-8")
        assert detector_main.main(["--detector-config", str(config)]) == 2
        assert start("--min-windows", "1") == 2
        assert "min-windows at least 2" in capsys.readouterr().err
        assert server == []

    def test_a_token_file_that_cannot_be_read_or_is_too_short_is_exit_2(self, tmp_path: Path, capsys, server) -> None:
        assert start("--token-file", str(tmp_path / "missing")) == 2
        assert "cannot read --token-file" in capsys.readouterr().err
        short = tmp_path / "short"
        short.write_text("abc", encoding="utf-8")
        assert start("--token-file", str(short)) == 2
        assert "at least 16 characters" in capsys.readouterr().err
        assert server == []

    def test_a_short_token_in_the_environment_is_refused_too(self, monkeypatch, capsys, server) -> None:
        monkeypatch.setenv("HYDRA_UMC_ANOMALY_TOKEN", "short")
        assert start() == 2
        assert "at least 16 characters" in capsys.readouterr().err

    def test_an_address_that_is_not_loopback_needs_a_token(self, capsys, server) -> None:
        for addr in ("0.0.0.0", "192.168.0.9", "not-an-address"):
            assert start("--addr", addr) == 2
            assert "is not loopback" in capsys.readouterr().err
        assert server == []

    def test_localhost_and_ipv6_loopback_do_not_need_one(self, server) -> None:
        assert start("--addr", "localhost") == 0
        assert start("--addr", "::1") == 0
        assert [s.address[0] for s in server] == ["localhost", "::1"]

    def test_an_identity_key_file_that_cannot_be_read_is_exit_2(self, tmp_path: Path, capsys, server) -> None:
        assert start("--identity-key-file", str(tmp_path / "missing.key")) == 2
        assert capsys.readouterr().err.startswith("error:")
        assert server == []

    def test_an_identity_key_lets_a_foreign_address_start_with_nothing_open(self, tmp_path: Path, server) -> None:
        key = tmp_path / "key"
        key.write_text("k" * 40, encoding="utf-8")
        assert start("--addr", "0.0.0.0", "--identity-key-file", str(key)) == 0
        assert server[0].options["protect_reads"] is True and server[0].identity_key is not None

    def test_an_end_that_is_both_inclusive_and_exclusive_is_exit_2(self, capsys, server) -> None:
        assert start("--train-end", "10", "--train-before", "10") == 2
        assert "not both" in capsys.readouterr().err

    @pytest.mark.parametrize("given", [(), ("--source-id", "a"), ("--source-id", "a", "--kind", "k"), ("--source-id", "a", "--kind", "k", "--field", "f")])
    def test_the_data_lake_options_go_together(self, given, capsys, server) -> None:
        assert start("--datalake-url", "http://127.0.0.1:1", *given) == 2
        assert "requires --source-id, --kind, --field and --window-size" in capsys.readouterr().err
        assert server == []


def healthy_windows(count: int = 4, size: int = 64):
    rng = np.random.default_rng(7)
    return [np.sin(np.linspace(0, 20 * np.pi, size)) + 0.05 * rng.standard_normal(size) for _ in range(count)]


LAKE = ("--datalake-url", "http://127.0.0.1:1", "--source-id", "a", "--kind", "b", "--field", "c", "--window-size", "64", "--legacy-baseline")


class TestFitFromTheDataLake:
    def fetched(self, monkeypatch, truncated=False, windows=None):
        result = SimpleNamespace(windows=healthy_windows() if windows is None else windows, truncated=truncated, summary=lambda: "4 windows")
        monkeypatch.setattr(detector_main, "fetch_windows_checked", lambda *a, **k: result)

    def test_a_fit_that_works_is_reported_and_the_baseline_is_saved(self, monkeypatch, tmp_path: Path, capsys, server) -> None:
        self.fetched(monkeypatch)
        path = tmp_path / "baseline.npz"
        assert start(*LAKE, "--baseline-path", str(path)) == 0
        captured = capsys.readouterr()
        assert "read from DATALAKE: 4 windows" in captured.out and "fit 4 window(s) from DATALAKE" in captured.out
        assert "persisted fresh baseline" in captured.out and path.is_file() and captured.err == ""
        assert server[0].detector.baseline is not None
        # the next start restores what was persisted
        assert start("--legacy-baseline", "--baseline-path", str(path)) == 0
        assert "restored baseline from" in capsys.readouterr().out

    def test_a_history_longer_than_the_limit_is_flagged(self, monkeypatch, capsys, server) -> None:
        self.fetched(monkeypatch, truncated=True)
        assert start(*LAKE) == 0
        assert "WARNING: the history is longer than --max-train-points" in capsys.readouterr().err

    def test_a_baseline_that_cannot_be_saved_is_exit_1(self, monkeypatch, tmp_path: Path, capsys, server) -> None:
        self.fetched(monkeypatch)
        assert start(*LAKE, "--baseline-path", str(tmp_path / "no" / "such" / "folder" / "baseline.npz")) == 1
        assert "could not save the baseline" in capsys.readouterr().err
        assert server == []

    def test_windows_the_detector_cannot_fit_are_exit_1(self, monkeypatch, capsys, server) -> None:
        self.fetched(monkeypatch, windows=healthy_windows(count=1))
        assert start(*LAKE) == 1
        assert "could not fit from DATALAKE" in capsys.readouterr().err


class TestSeriesRestoredFromAFolder:
    def test_the_restored_count_and_the_problems_are_said(self, monkeypatch, tmp_path: Path, capsys, server) -> None:
        class Registry:
            def __init__(self, factory, folder, limit) -> None:
                self.folder, self.limit = folder, limit

            def restore_all(self):
                return 2, ["a.npz is damaged"]

        monkeypatch.setattr(detector_main, "DetectorRegistry", Registry)
        assert start("--baseline-dir", str(tmp_path), "--max-series", "7") == 0
        captured = capsys.readouterr()
        assert f"restored the baseline of 2 series from {tmp_path}" in captured.out
        assert "WARNING: could not restore a series: a.npz is damaged" in captured.err
        assert server[0].options["registry"].limit == 7


class TestFollowMode:
    def follow(self, monkeypatch, *, fleet: bool, mqtt: bool, mode: str = "propose"):
        config = SimpleNamespace(
            mqtt=object() if mqtt else None, fleet=SimpleNamespace(enabled=fleet), maintenance=SimpleNamespace(mode=mode),
            series=["s1", "s2"], datalake_url="http://127.0.0.1:1", interval_s=5.0, write_scores=True, score_kind="anomaly_score",
        )
        events = {"stopped": False, "logs": []}

        class Stop:
            def set(self):
                events["stopped"] = True

        class Follower:
            def __init__(self, cfg, registry, client, sample_rate, publisher, fleet):
                events["publisher"], events["fleet"] = publisher, fleet

            snapshot = staticmethod(lambda: {"snap": 1})
            fleet_snapshot = staticmethod(lambda: {"fleet": 1})

            def start(self):
                return object(), Stop()

        class Tracker:
            def __init__(self, fleet_cfg, maintenance, reference_for, log):
                events["reference_for"], events["log"] = reference_for, log

        monkeypatch.setattr(detector_main, "load_follow_config", lambda path: (config, {}))
        monkeypatch.setattr(detector_main, "_follow_client", lambda cfg, raw: object())
        monkeypatch.setattr(detector_main, "MqttPublisher", lambda cfg: "publisher")
        monkeypatch.setattr(detector_main, "Follower", Follower)
        monkeypatch.setattr(detector_main, "FleetTracker", Tracker)
        return events

    def test_follow_with_alarms_and_the_fleet_view(self, monkeypatch, capsys, server) -> None:
        events = self.follow(monkeypatch, fleet=True, mqtt=True)
        assert start("--follow-config", "ignored.json") == 0
        out = capsys.readouterr().out
        assert "fleet view is on (GET /fleet); maintenance: propose (only PROPOSES to the dispatcher; a person decides)" in out
        assert "following 2 series" in out and "written back as anomaly_score" in out and "alarms over MQTT" in out
        assert events["publisher"] == "publisher" and events["stopped"] is True
        assert server[0].follower_snapshot() == {"snap": 1} and server[0].fleet_snapshot() == {"fleet": 1}
        # the fleet reference comes from the drift monitor of a series, and is None for a series that has none
        assert events["reference_for"](("nobody", "k", "f")) is None
        events["log"]("a line")
        assert "[anomaly-detector] a line" in capsys.readouterr().err

    def test_follow_without_alarms_or_a_fleet_view(self, monkeypatch, capsys, server) -> None:
        events = self.follow(monkeypatch, fleet=False, mqtt=False)
        assert start("--follow-config", "ignored.json") == 0
        out = capsys.readouterr().out
        assert "fleet view" not in out and "alarms not published" in out
        assert events["publisher"] is None and events["fleet"] is None and not hasattr(server[0], "fleet_snapshot")

    def test_the_fleet_view_in_another_mode_does_not_say_it_only_proposes(self, monkeypatch, capsys, server) -> None:
        self.follow(monkeypatch, fleet=True, mqtt=False, mode="off")
        assert start("--follow-config", "ignored.json") == 0
        assert "PROPOSES" not in capsys.readouterr().out

    def test_a_follow_file_that_cannot_be_used_is_exit_2_and_the_server_is_closed(self, monkeypatch, capsys, server) -> None:
        def refuse(path):
            raise FollowConfigError("no series")

        monkeypatch.setattr(detector_main, "load_follow_config", refuse)
        assert start("--follow-config", "ignored.json") == 2
        assert "--follow-config: no series" in capsys.readouterr().err
        assert server[0].closed
