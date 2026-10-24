# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - src/hydra_umc_anomaly_detector/datalake_client.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""A real HTTP client for HYDRA-UMC-DATALAKE's own API
(src/hydra_umc_datalake/api.py). This project's own README describes running
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
        read_token: str | None = None,
        ingest_token: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # DATALAKE asks for a bearer token per role when it has one configured (read for GET /query, ingest for POST /ingest);
        # a client without one still works against a DATALAKE that has none.
        self.read_token = read_token or None
        self.ingest_token = ingest_token or None
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
                request = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.read_token}"} if self.read_token else {})
                with urllib.request.urlopen(request, timeout=self.timeout_s) as resp:
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

    def ingest(self, *, source_id: str, kind: str, timestamp: int, fields: dict[str, float]) -> None:
        """Writes one sample (POST /ingest). Not retried: the follower tries again with the next window, and DATALAKE's point
        identity makes a repeated write harmless."""
        body = json.dumps({"sourceId": source_id, "kind": kind, "timestamp": timestamp, "fields": fields}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.ingest_token:
            headers["Authorization"] = f"Bearer {self.ingest_token}"
        request = urllib.request.Request(f"{self.base_url}/ingest", data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            raise DatalakeError(f"DATALAKE returned HTTP {e.code} for /ingest: {e.read().decode('utf-8', errors='replace')}") from e
        except urllib.error.URLError as e:
            raise DatalakeError(f"could not reach DATALAKE at {self.base_url}: {e.reason}") from e

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


@dataclass(frozen=True)
class WindowFetch:
    """What fetch_windows_checked() found: the windows, and what it had to leave out to make them honest."""

    windows: list[list[float]]
    points: int  # distinct points read (after removing repeated timestamps)
    pages: int
    duplicates: int  # points whose timestamp repeated one already read (the first is kept)
    gaps: int  # places where the spacing was not the one expected: the series is cut there
    dropped_samples: int  # points that fit no complete window (the tail of each run)
    truncated: bool  # the read stopped at max_points: there is more history than was read

    def summary(self) -> str:
        text = f"{self.points} point(s) in {self.pages} page(s) -> {len(self.windows)} window(s)"
        if self.duplicates:
            text += f"; {self.duplicates} repeated timestamp(s) removed"
        if self.gaps:
            text += f"; the series was cut at {self.gaps} irregular spacing(s), {self.dropped_samples} sample(s) left out"
        if self.truncated:
            text += "; STOPPED at the point limit, there is more history than was read"
        return text


def split_runs(points: list[Point], sample_rate: float, tolerance: float = 0.5) -> list[list[Point]]:
    """Cuts a series (ascending timestamps in milliseconds) wherever two neighbours are not about 1/sample_rate apart, so each run is
    evenly sampled. Always returns at least one run (possibly empty)."""
    period_ms = 1000.0 / sample_rate
    low, high = period_ms * (1.0 - tolerance), period_ms * (1.0 + tolerance)
    runs: list[list[Point]] = []
    current: list[Point] = []
    for index, point in enumerate(points):
        if index and not low <= point.timestamp - points[index - 1].timestamp <= high:
            runs.append(current)
            current = []
        current.append(point)
    runs.append(current)
    return runs


def fetch_windows_checked(
    client: DatalakeClient,
    *,
    source_id: str,
    kind: str,
    field: str,
    window_size: int,
    sample_rate: float,
    start: int | None = None,
    end: int | None = None,
    page_size: int = 10000,
    max_points: int = 200_000,
    tolerance: float = 0.5,
) -> WindowFetch:
    """Like fetch_windows(), but the timestamps are looked at.

    A window goes into an FFT as if its samples were evenly spaced at `sample_rate`; a series with a gap, a
    repeated timestamp or a wandering interval gives a spectrum that describes the holes, not the machine. So:

      * the read is paged (DATALAKE answers the OLDEST points first, up to `page_size`): the next page starts
        after the last timestamp read, until a short page or `max_points`; hitting `max_points` is reported
        (`truncated`), never silent;
      * a repeated timestamp keeps its first point;
      * the series is cut wherever two neighbours are not about 1/sample_rate apart (more than `tolerance` of
        a period off, timestamps in milliseconds), and windows are made inside each uninterrupted run only.
    """
    if window_size <= 0:
        raise ValueError(f"window_size must be positive, got {window_size}")
    if sample_rate <= 0:
        raise ValueError(f"sample_rate must be positive, got {sample_rate}")
    if page_size <= 0 or max_points <= 0:
        raise ValueError("page_size and max_points must be positive")
    if not 0 <= tolerance < 1:
        raise ValueError("tolerance must be at least 0 and below 1")

    points: list[Point] = []
    pages = 0
    cursor = start
    duplicates = 0
    truncated = False
    last_timestamp: int | None = None
    while True:
        page = client.query(source_id=source_id, kind=kind, field=field, start=cursor, end=end, limit=page_size)
        pages += 1
        for point in page:
            if last_timestamp is not None and point.timestamp <= last_timestamp:
                duplicates += 1  # ascending order is DATALAKE's contract: <= is a repeat
                continue
            points.append(point)
            last_timestamp = point.timestamp
        if len(page) < page_size:
            break
        if len(points) >= max_points:
            truncated = True
            break
        cursor = page[-1].timestamp + 1

    point_runs = split_runs(points, sample_rate, tolerance)
    gaps = len(point_runs) - 1
    runs = [[p.value for p in run] for run in point_runs]

    windows: list[list[float]] = []
    dropped = 0
    for run in runs:
        count = len(run) // window_size
        windows.extend(run[i * window_size : (i + 1) * window_size] for i in range(count))
        dropped += len(run) - count * window_size
    return WindowFetch(
        windows=windows, points=len(points), pages=pages, duplicates=duplicates, gaps=gaps,
        dropped_samples=dropped, truncated=truncated,
    )


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
