# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - src/hydra_umc_anomaly_detector/datalake_client.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""A real HTTP client for HYDRA-UMC-DATALAKE's own API
(src/hydra_umc_datalake/api.py) - found missing in an ecosystem-wide
software-improvements audit: this project's own README describes running
"on telemetry HYDRA-UMC-TELEMETRY-COLLECTOR already wrote there", but no
code anywhere in this repo ever actually queried DATALAKE - today that
meant pasting raw arrays into POST /baseline/fit / POST /detect by hand.

Same shape (and the same retry-on-transient-network-failure fix, already
landed this session) as HYDRA-UMC-PRODUCTION-REPORTS' own
datalake_client.py - a deliberate, consistent pattern across every real
DATALAKE consumer in this family, not reinvented per repo. Deliberately
just a thin wrapper over DATALAKE's real GET /query (stdlib urllib, no
new dependency) - this repo does not import HYDRA-UMC-DATALAKE's own
Python package directly (separate repos/services on purpose).
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable


class DatalakeError(RuntimeError):
    """Raised when a real request to DATALAKE fails or DATALAKE itself
    reports an error - never swallowed silently, since a baseline fit on
    a request that silently returned nothing would look like "no data"
    instead of "couldn't ask"."""


@dataclass(frozen=True)
class Point:
    """Mirrors HYDRA-UMC-DATALAKE's own query response shape exactly
    (src/hydra_umc_datalake/store.py's Point, serialized by its api.py)."""

    source_id: str
    kind: str
    field: str
    timestamp: int
    value: float


class DatalakeClient:
    """A real client against one running HYDRA-UMC-DATALAKE instance."""

    def __init__(
        self,
        base_url: str,
        timeout_s: float = 10.0,
        *,
        max_attempts: int = 3,
        retry_delay_seconds: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        # Same reasoning as HYDRA-UMC-PRODUCTION-REPORTS' own client: a
        # timeout alone doesn't cover a transient network hiccup (a
        # dropped connection, a momentary DNS blip, DATALAKE
        # mid-restart) - only urllib.error.URLError is retried, never a
        # real HTTPError DATALAKE itself already answered with.
        self.max_attempts = max_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self._sleep = sleep

    def _get(self, path: str, params: dict[str, str | int]) -> object:
        query = urllib.parse.urlencode(params)
        url = f"{self.base_url}{path}?{query}"
        last_error: urllib.error.URLError | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                with urllib.request.urlopen(url, timeout=self.timeout_s) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                raise DatalakeError(f"DATALAKE returned HTTP {e.code} for {path}: {body}") from e
            except urllib.error.URLError as e:
                last_error = e
                if attempt < self.max_attempts:
                    self._sleep(self.retry_delay_seconds)
        raise DatalakeError(
            f"could not reach DATALAKE at {self.base_url} after {self.max_attempts} attempts: {last_error.reason}"
        ) from last_error

    def query(
        self,
        *,
        source_id: str | None = None,
        kind: str | None = None,
        field: str | None = None,
        start: int | None = None,
        end: int | None = None,
        limit: int = 10000,
    ) -> list[Point]:
        """Real range query against DATALAKE's own GET /query. Points
        come back sorted by timestamp ASC (store.py's own convention)."""
        params: dict[str, str | int] = {"limit": limit}
        if source_id is not None:
            params["sourceId"] = source_id
        if kind is not None:
            params["kind"] = kind
        if field is not None:
            params["field"] = field
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end

        raw = self._get("/query", params)
        return [
            Point(
                source_id=p["sourceId"],
                kind=p["kind"],
                field=p["field"],
                timestamp=p["timestamp"],
                value=p["value"],
            )
            for p in raw
        ]


def fetch_windows(
    client: DatalakeClient,
    *,
    source_id: str,
    kind: str,
    field: str,
    window_size: int,
    start: int | None = None,
    end: int | None = None,
    limit: int = 10000,
) -> list[list[float]]:
    """Fetches real points for one (source_id, kind, field) series and
    chunks them into consecutive, non-overlapping windows of exactly
    `window_size` samples each, in the real timestamp order DATALAKE
    already returns them in. A trailing partial window (fewer than
    `window_size` samples left over) is dropped - `fit_baseline()`/
    `AnomalyDetector.score()` both require every window to be the same
    length, so a short final window would either crash or (worse)
    silently compare mismatched bin counts.
    """
    if window_size <= 0:
        raise ValueError(f"window_size must be positive, got {window_size}")
    points = client.query(source_id=source_id, kind=kind, field=field, start=start, end=end, limit=limit)
    values = [p.value for p in points]
    window_count = len(values) // window_size
    return [values[i * window_size : (i + 1) * window_size] for i in range(window_count)]
