# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - tests/test_main_start.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The start of the service: where the tokens of the data lake come from, what the detector configuration file may hold, every option combination that stops the
start with exit 2 before anything listens, a fit from the data lake that fails (exit 1), and the normal start with a stand-in server that never listens."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hydra_umc_anomaly_detector import main as detector_main
from hydra_umc_anomaly_detector.follower import FollowConfigError

NAMES = ("HYDRA_UMC_DATALAKE_READ_TOKEN", "HYDRA_UMC_DATALAKE_INGEST_TOKEN", "HYDRA_UMC_DATALAKE_TOKEN")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in NAMES + tuple(name + "_FILE" for name in NAMES) + ("HYDRA_UMC_ANOMALY_PROTECT_READS", "HYDRA_UMC_ANOMALY_TOKEN"):
        monkeypatch.delenv(name, raising=False)


class TestSecrets:
    def test_the_file_named_by_the_configuration_wins(self, tmp_path: Path, monkeypatch) -> None:
        token_file = tmp_path / "read"
        token_file.write_text("  from-the-file \n", encoding="utf-8")
        monkeypatch.setenv(NAMES[0], "from-the-environment")
        assert detector_main._secret({"readTokenFile": str(token_file)}, "readTokenFile", NAMES[:1]) == "from-the-file"

    @pytest.mark.parametrize("value", ["", 7, ["x"]])
    def test_a_file_name_that_is_not_a_path_is_refused(self, value) -> None:
        with pytest.raises(FollowConfigError, match="must be the path of a file"):
            detector_main._secret({"readTokenFile": value}, "readTokenFile", NAMES)

    def test_a_file_that_cannot_be_read_is_refused_with_the_reason(self, tmp_path: Path) -> None:
        with pytest.raises(FollowConfigError, match="cannot read datalake.readTokenFile"):
            detector_main._secret({"readTokenFile": str(tmp_path / "missing")}, "readTokenFile", NAMES)

    def test_the_first_variable_that_is_set_is_used(self, monkeypatch) -> None:
        monkeypatch.setenv(NAMES[1], " second ")
        monkeypatch.setenv(NAMES[2], "third")
        assert detector_main._secret({}, "k", (NAMES[0], NAMES[1], NAMES[2])) == "second"

    def test_a_variable_with_the_file_suffix_names_a_file(self, tmp_path: Path, monkeypatch) -> None:
        token_file = tmp_path / "token"
        token_file.write_text("from-a-file\n", encoding="utf-8")
        monkeypatch.setenv(NAMES[2] + "_FILE", str(token_file))
        assert detector_main._secret({}, "k", NAMES[2:]) == "from-a-file"

    def test_a_suffixed_variable_naming_a_missing_file_is_refused(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv(NAMES[2] + "_FILE", str(tmp_path / "missing"))
        with pytest.raises(FollowConfigError, match="_FILE"):
            detector_main._secret({}, "k", NAMES[2:])

    def test_nothing_set_is_no_token(self) -> None:
        assert detector_main._secret({}, "k", NAMES) is None

    def test_the_follow_client_reads_both_tokens(self, monkeypatch) -> None:
        monkeypatch.setenv(NAMES[2], "shared")
        client = detector_main._follow_client(SimpleNamespace(datalake_url="http://127.0.0.1:1"), {})
        assert client.__dict__.get("read_token", client.__dict__.get("_read_token")) == "shared"


class TestDetectorConfigurationFile:
    def run(self, tmp_path: Path, content: str | None, *extra: str) -> int:
        path = tmp_path / "detector.json"
        if content is not None:
            path.write_text(content, encoding="utf-8")
        return detector_main.main(["--detector-config", str(path), *extra])

    def test_a_missing_file_is_exit_2(self, tmp_path: Path, capsys) -> None:
        assert self.run(tmp_path, None) == 2
        assert "--detector-config" in capsys.readouterr().err

    def test_a_file_that_is_not_json_is_exit_2(self, tmp_path: Path) -> None:
        assert self.run(tmp_path, "{not json") == 2

    def test_a_json_value_that_is_not_an_object_is_exit_2(self, tmp_path: Path, capsys) -> None:
        assert self.run(tmp_path, "[1, 2]") == 2
        assert "JSON object" in capsys.readouterr().err

    def test_an_unknown_key_is_named_and_is_exit_2(self, tmp_path: Path, capsys) -> None:
        assert self.run(tmp_path, json.dumps({"treshold": 3})) == 2
        assert "treshold" in capsys.readouterr().err

    def test_a_rul_flag_that_is_not_a_boolean_is_exit_2(self, tmp_path: Path, capsys) -> None:
        assert self.run(tmp_path, json.dumps({"rul": "yes"})) == 2
        assert "rul must be true or false" in capsys.readouterr().err

    @pytest.mark.parametrize("points", [2, True, 7.5])
    def test_a_rul_minimum_below_three_or_not_an_integer_is_exit_2(self, tmp_path: Path, points) -> None:
        assert self.run(tmp_path, json.dumps({"rulMinPoints": points})) == 2

    def test_an_unknown_scoring_is_exit_2(self, tmp_path: Path, capsys) -> None:
        assert self.run(tmp_path, json.dumps({"scoring": "vibes"})) == 2
        assert "scoring must be one of" in capsys.readouterr().err


class TestOptionsThatStopTheStart:
    def test_protecting_the_reads_without_any_credential_is_exit_2(self, capsys) -> None:
        assert detector_main.main(["--protect-reads"]) == 2
        assert "--protect-reads needs a token" in capsys.readouterr().err

    def test_the_environment_can_ask_for_protected_reads_too(self, monkeypatch, capsys) -> None:
        monkeypatch.setenv("HYDRA_UMC_ANOMALY_PROTECT_READS", "1")
        assert detector_main.main([]) == 2
        assert "--protect-reads needs a token" in capsys.readouterr().err

    def test_fewer_than_one_series_is_exit_2(self, capsys) -> None:
        assert detector_main.main(["--max-series", "0"]) == 2
        assert "--max-series must be at least 1" in capsys.readouterr().err

    def test_a_follow_configuration_that_cannot_be_read_is_exit_2_and_the_server_is_closed(self, tmp_path: Path, monkeypatch, capsys) -> None:
        closed: list[bool] = []

        class Server:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def server_close(self) -> None:
                closed.append(True)

        monkeypatch.setattr(detector_main, "DetectorServer", Server)
        assert detector_main.main(["--follow-config", str(tmp_path / "missing.json")]) == 2
        assert "--follow-config" in capsys.readouterr().err
        assert closed == [True]


class TestFitFromTheDataLake:
    def test_a_fit_that_fails_is_exit_1_with_the_reason(self, monkeypatch, capsys) -> None:
        def refuse(*args, **kwargs):
            raise detector_main.DatalakeError("the data lake does not answer")

        monkeypatch.setattr(detector_main, "fetch_windows_checked", refuse)
        assert detector_main.main(["--datalake-url", "http://127.0.0.1:1", "--source-id", "a", "--kind", "b", "--field", "c", "--window-size", "64"]) == 1
        assert "could not fit from DATALAKE" in capsys.readouterr().err

    def test_a_baseline_file_that_cannot_be_restored_is_exit_1(self, tmp_path: Path, capsys) -> None:
        broken = tmp_path / "baseline.npz"
        broken.write_bytes(b"this is not a baseline")
        assert detector_main.main(["--baseline-path", str(broken)]) == 1
        assert "could not restore baseline" in capsys.readouterr().err


class TestNormalStart:
    def stand_in(self, monkeypatch):
        made: list[SimpleNamespace] = []

        class Server:
            def __init__(self, address, detector, baseline_path, **options) -> None:
                self.address, self.options, self.closed, self.health_open = address, options, False, None
                made.append(self)

            def serve_forever(self) -> None:
                raise KeyboardInterrupt

            def server_close(self) -> None:
                self.closed = True

        monkeypatch.setattr(detector_main, "DetectorServer", Server)
        return made

    def test_a_plain_start_listens_on_loopback_and_closes(self, monkeypatch, capsys) -> None:
        made = self.stand_in(monkeypatch)
        assert detector_main.main(["--port", "0"]) == 0
        out = capsys.readouterr().out
        assert "HTTP API listening on 127.0.0.1:0" in out and "open (loopback only)" in out and "shutting down" in out
        assert made[0].closed and made[0].options["protect_reads"] is False

    def test_a_token_in_the_environment_protects_the_reads_by_default(self, tmp_path: Path, monkeypatch) -> None:
        made = self.stand_in(monkeypatch)
        token_file = tmp_path / "token"
        token_file.write_text("a-token-of-at-least-sixteen-characters", encoding="utf-8")
        assert detector_main.main(["--port", "0", "--token-file", str(token_file)]) == 0
        assert made[0].options["protect_reads"] is True

    def test_the_reads_can_be_left_open_on_purpose(self, tmp_path: Path, monkeypatch) -> None:
        made = self.stand_in(monkeypatch)
        token_file = tmp_path / "token"
        token_file.write_text("a-token-of-at-least-sixteen-characters", encoding="utf-8")
        assert detector_main.main(["--port", "0", "--token-file", str(token_file), "--no-protect-reads"]) == 0
        assert made[0].options["protect_reads"] is False

    def test_the_environment_can_leave_them_open_too(self, tmp_path: Path, monkeypatch) -> None:
        made = self.stand_in(monkeypatch)
        monkeypatch.setenv("HYDRA_UMC_ANOMALY_PROTECT_READS", "0")
        token_file = tmp_path / "token"
        token_file.write_text("a-token-of-at-least-sixteen-characters", encoding="utf-8")
        assert detector_main.main(["--port", "0", "--token-file", str(token_file)]) == 0
        assert made[0].options["protect_reads"] is False

    def test_the_identity_of_the_server_opens_nothing(self, tmp_path: Path, monkeypatch, capsys) -> None:
        made = self.stand_in(monkeypatch)
        key_file = tmp_path / "key"
        key_file.write_text("k" * 40, encoding="utf-8")
        assert detector_main.main(["--port", "0", "--identity-key-file", str(key_file)]) == 0
        assert made[0].options["protect_reads"] is True
        assert "nothing is open" in capsys.readouterr().out

    def test_the_configuration_file_sets_the_threshold(self, tmp_path: Path, monkeypatch) -> None:
        made = self.stand_in(monkeypatch)
        config = tmp_path / "detector.json"
        config.write_text(json.dumps({"threshold": 4.5, "rul": True, "rulMinPoints": 5}), encoding="utf-8")
        assert detector_main.main(["--port", "0", "--detector-config", str(config)]) == 0
        assert made[0].options["rul_enabled"] is True and made[0].options["rul_min_points"] == 5
