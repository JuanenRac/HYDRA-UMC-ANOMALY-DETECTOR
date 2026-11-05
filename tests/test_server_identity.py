# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - tests/test_server_identity.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The identity of HYDRA-UMC-SERVER (server_identity.py): off by default; on, a signed token of the server is accepted and nothing is open."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from hydra_umc_anomaly_detector import server_identity
from hydra_umc_anomaly_detector.api import DetectorServer
from hydra_umc_anomaly_detector.detector import AnomalyDetector

KEY = b"a-signing-key-of-the-server-0123456789"
TOKEN = "own-token-of-the-service"


def _serve(token: str | None, key: bytes | None, health_open: bool = False) -> Iterator[tuple[str, DetectorServer]]:
    server = DetectorServer(("127.0.0.1", 0), AnomalyDetector(sample_rate=1000, threshold=4.0), token=token)
    if key is not None:
        server.identity_key = key
        server.protect_reads = True  # what the command does when the key is given
    server.health_open = health_open
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _call(url: str, method: str = "GET", bearer: str | None = None) -> int:
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    request = urllib.request.Request(url, data=b"{}" if method == "POST" else None, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status
    except urllib.error.HTTPError as error:
        error.read()
        return error.code


def test_it_is_off_by_default() -> None:
    assert DetectorServer.identity_key is None
    for base, _ in _serve(None, None):
        assert _call(base + "/stats") == 200
        assert _call(base + "/stats", bearer=server_identity.sign_token(KEY)) == 200  # nothing is asked while it is off
    for base, _ in _serve(TOKEN, None):
        assert _call(base + "/detectors", bearer=server_identity.sign_token(KEY)) == 401  # a token of the SERVER means nothing while it is off


def test_with_the_identity_on_nothing_is_open_not_even_stats_or_health() -> None:
    for base, _ in _serve(None, KEY, health_open=True):
        for path in ("/stats", "/health", "/detectors", "/metrics"):
            assert _call(base + path) == 401, path
        assert _call(base + "/stats", bearer="not-a-token") == 401
        viewer = server_identity.sign_token(KEY, role="viewer")
        assert _call(base + "/stats", bearer=viewer) == 200
        assert _call(base + "/health", bearer=viewer) == 200
        assert _call(base + "/detectors", bearer=viewer) == 200
        assert _call(base + "/baseline/fit", "POST", viewer) == 401  # a viewer reads but does not change
        assert _call(base + "/baseline/fit", "POST", server_identity.sign_token(KEY, role="operator")) == 400  # an operator gets in (the body is empty)
        other = server_identity.sign_token(b"another-key-of-more-than-32-characters!")
        assert _call(base + "/stats", bearer=other) == 401


def test_the_own_token_keeps_working_with_the_identity_on() -> None:
    for base, _ in _serve(TOKEN, KEY):
        assert _call(base + "/baseline/fit", "POST", TOKEN) == 400
        assert _call(base + "/stats", bearer=TOKEN) == 200


def test_the_key_comes_from_a_file(tmp_path: Path) -> None:
    key_file = tmp_path / "identity.key"
    key_file.write_text(KEY.decode(), encoding="utf-8")
    assert server_identity.identity_key_from(str(key_file)) == KEY
    assert server_identity.identity_key_from(None, {}) is None
    assert server_identity.ENV_IDENTITY_KEY_FILE == "HYDRA_UMC_ANOMALY_DETECTOR_IDENTITY_KEY_FILE"
    assert json.loads(json.dumps({"ok": True}))["ok"]


def test_the_copy_of_the_shared_module_is_the_canonical_one() -> None:
    canonical = Path(__file__).resolve().parents[2] / "HYDRA-UMC-SDK" / "tools" / "canonical" / "server_identity.py"
    if not canonical.is_file():
        pytest.skip("the SDK checkout is not next to this repository")
    assert Path(server_identity.__file__).read_bytes().replace(b"\r\n", b"\n") == canonical.read_bytes().replace(b"\r\n", b"\n")
