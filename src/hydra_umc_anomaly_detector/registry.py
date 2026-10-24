# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - src/hydra_umc_anomaly_detector/registry.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""One detector per series: a baseline for each sensor, not one for the whole plant.

The original routes keep ONE shared baseline (`/baseline/fit`, `/detect`). A motor and a spindle do not vibrate alike, and a
single baseline fitted on one of them calls the other anomalous. `DetectorRegistry` holds a detector for each
`(sourceId, kind, field)`, the same triple HYDRA-UMC-DATALAKE stores a series under, each with its own baseline, its own model
version, its own drift monitor and its own lock, so fitting or scoring one series never waits for, nor changes, another.

The registry is bounded (`max_detectors`): a client that is allowed to create series cannot make this process grow without end.
A series exists once it has been fitted; scoring an unknown series is "not fitted", never a verdict from some other baseline.
Baselines are saved in a directory (one `.npz` and one small `.key.json` per series, named by a digest of the key, so a hostile
sourceId can never become a path) and restored from it at start.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, NamedTuple

from .baseline import BaselineError, load_baseline
from .detector import AnomalyDetector
from .drift import DriftMonitor

DEFAULT_MAX_DETECTORS = 64
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")


class SeriesKey(NamedTuple):
    source_id: str
    kind: str
    field: str

    def digest(self) -> str:
        return hashlib.sha256("\0".join(self).encode("utf-8")).hexdigest()[:40]


class RegistryError(ValueError):
    """The key is not a series name, or the registry cannot take another series."""


class RegistryFull(RegistryError):
    """Another series would exceed the bound."""


def parse_key(source_id: str, kind: str, field_name: str) -> SeriesKey:
    for label, value in (("sourceId", source_id), ("kind", kind), ("field", field_name)):
        if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
            raise RegistryError(f"{label} must be 1 to 96 characters of letters, digits, dot, dash, colon or underscore, starting with a letter or digit")
    return SeriesKey(source_id, kind, field_name)


@dataclass
class Entry:
    """One series: the same attributes the shared detector of the server has, so one set of handlers serves both."""

    detector: AnomalyDetector
    baseline_path: Path | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    drift_monitor: DriftMonitor | None = None
    drift_model_version: int = 0


class DetectorRegistry:
    def __init__(self, factory: Callable[[], AnomalyDetector], baseline_dir: Path | None = None, max_detectors: int = DEFAULT_MAX_DETECTORS) -> None:
        if max_detectors < 1:
            raise ValueError("max_detectors must be at least 1")
        self._factory = factory
        self._dir = Path(baseline_dir) if baseline_dir is not None else None
        self._max = max_detectors
        self._lock = threading.Lock()
        self._entries: dict[SeriesKey, Entry] = {}

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def get(self, key: SeriesKey) -> Entry | None:
        with self._lock:
            return self._entries.get(key)

    def new_detector(self) -> AnomalyDetector:
        """A detector with this server's sample rate, threshold and window, not yet in the registry."""
        return self._factory()

    def insert(self, key: SeriesKey, detector: AnomalyDetector) -> Entry:
        """Put a series in the registry (a first fit that succeeded) or return the entry that is already there.

        Only a detector that was fitted is ever inserted, so a request that fails to fit never takes one of the places.
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                return entry
            if len(self._entries) >= self._max:
                raise RegistryFull(f"{self._max} series already have a detector; this server holds no more")
            path = None
            if self._dir is not None:
                self._dir.mkdir(parents=True, exist_ok=True)
                path = self._dir / f"{key.digest()}.npz"
                self._write_key(key)
            entry = Entry(detector, path)
            self._entries[key] = entry
            return entry

    def listing(self) -> list[dict[str, object]]:
        with self._lock:
            items = sorted(self._entries.items())
        rows = []
        for key, entry in items:
            with entry.lock:
                rows.append({
                    "sourceId": key.source_id, "kind": key.kind, "field": key.field,
                    "fitted": entry.detector.is_fitted, "modelVersion": entry.detector.model_version,
                })
        return rows

    def _write_key(self, key: SeriesKey) -> None:
        assert self._dir is not None
        path = self._dir / f"{key.digest()}.key.json"
        if path.exists():
            return
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps({"sourceId": key.source_id, "kind": key.kind, "field": key.field}), encoding="utf-8")
        os.replace(temporary, path)

    def restore_all(self) -> tuple[int, list[str]]:
        """Restore every series saved in the directory. Returns how many were restored and a message for each that could not be."""
        if self._dir is None or not self._dir.is_dir():
            return 0, []
        restored, problems = 0, []
        for key_file in sorted(self._dir.glob("*.key.json")):
            try:
                raw = json.loads(key_file.read_text(encoding="utf-8"))
                key = parse_key(raw["sourceId"], raw["kind"], raw["field"])
                if key.digest() + ".key.json" != key_file.name:
                    raise RegistryError("the name of the file is not the digest of its key")
                baseline, model_version = load_baseline(self._dir / f"{key.digest()}.npz")
                with self._lock:
                    if len(self._entries) >= self._max:
                        raise RegistryFull(f"{self._max} series already have a detector")
                    entry = Entry(self._factory(), self._dir / f"{key.digest()}.npz")
                    entry.detector.restore(baseline, model_version)
                    self._entries[key] = entry
                restored += 1
            except (OSError, ValueError, KeyError, TypeError, BaselineError, json.JSONDecodeError) as error:
                problems.append(f"{key_file.name}: {error}")
        return restored, problems
