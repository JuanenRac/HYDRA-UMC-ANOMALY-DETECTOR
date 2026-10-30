# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - tests/conftest.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Most fixtures train on two or three short windows, which the default baseline now refuses (it asks for ten).
Those tests run with the earlier limits; the tests marked `strict_baseline` run with the real defaults."""
from __future__ import annotations

import pytest

from hydra_umc_anomaly_detector import baseline


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "strict_baseline: run with the default baseline limits (10 windows, ddof=1)")


@pytest.fixture(autouse=True)
def _small_baselines_for_fixtures(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("strict_baseline") is None:
        monkeypatch.setattr(baseline, "DEFAULT_MIN_WINDOWS", baseline.LEGACY_MIN_WINDOWS)
        monkeypatch.setattr(baseline, "DEFAULT_DDOF", baseline.LEGACY_DDOF)
