# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - tests/test_cut_answers.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Answers that are slow, reset, cut or not JSON. urlopen is replaced, so nothing leaves the machine."""
from __future__ import annotations

import http.client
import io
import urllib.request

import pytest

from hydra_umc_anomaly_detector.datalake_client import DatalakeClient, DatalakeError
from hydra_umc_anomaly_detector.fleet import MaintenanceConfig, MaintenanceProposer

FAILURES = [TimeoutError("timed out"), ConnectionResetError("reset"), http.client.IncompleteRead(b"[", 100), http.client.BadStatusLine("x")]


class CutAnswer(io.BytesIO):
    """An answer that stops in the middle of its body."""

    status = 200

    def read(self, *args):
        raise http.client.IncompleteRead(b"{", 100)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def failing(error):
    def opener(*args, **kwargs):
        raise error

    return opener


@pytest.mark.parametrize("error", FAILURES, ids=lambda e: type(e).__name__)
def test_a_failure_while_reading_is_retried_and_then_a_datalake_error(monkeypatch, error):
    monkeypatch.setattr(urllib.request, "urlopen", failing(error))
    waits = []
    client = DatalakeClient("http://127.0.0.1:1", max_attempts=3, sleep=waits.append)
    with pytest.raises(DatalakeError):
        client.query(source_id="motor-1")
    assert len(waits) == 2
    with pytest.raises(DatalakeError):
        client.ingest(source_id="motor-1", kind="score", timestamp=1, fields={"value": 1.0})


def test_a_cut_answer_followed_by_a_whole_one_is_read(monkeypatch):
    answers = [CutAnswer(), io.BytesIO(b"[]")]
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: answers.pop(0))
    assert DatalakeClient("http://127.0.0.1:1", sleep=lambda s: None).query(source_id="motor-1") == []


@pytest.mark.parametrize("body", [b"<html>portal</html>", b"\xff\xfe[", b'[{"sourceId": "mo'])
def test_an_answer_that_is_not_json_is_a_datalake_error(monkeypatch, body):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: io.BytesIO(body))
    with pytest.raises(DatalakeError, match="not JSON"):
        DatalakeClient("http://127.0.0.1:1").query(source_id="motor-1")


@pytest.mark.parametrize("error", FAILURES, ids=lambda e: type(e).__name__)
def test_a_proposal_that_cannot_be_delivered_is_an_os_error(monkeypatch, error):
    monkeypatch.setattr(urllib.request, "urlopen", failing(error))
    proposer = MaintenanceProposer(MaintenanceConfig(mode="propose", url="http://127.0.0.1:1"))
    with pytest.raises(OSError):
        proposer.propose("motor-1:vibration:value", "drift", 1.0, 2.0)
