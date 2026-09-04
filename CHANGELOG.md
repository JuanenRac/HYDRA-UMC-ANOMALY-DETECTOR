# Changelog

All notable work on **HYDRA-UMC-ANOMALY-DETECTOR** is summarized here, newest first. Full
session-by-session detail (including dates) lives in a private,
unpublished internal log - this file is public, so it intentionally
omits calendar dates.

## Versioning scheme

`pyproject.toml`'s `version` field bumps automatically on every real
build (`build.sh`/`.bat` - see `bump_version.py`, run as the first real
step of both scripts).

It follows the ecosystem-wide base-10 "odometer" rule rather than
semantic-versioning judgment calls:

- `PATCH` +1 on every build
- when `PATCH` would exceed 9, it resets to 0 and `MINOR` +1 instead (e.g. `0.0.9` -> `0.1.0`, never `0.0.10`)
- the same carry cascades into `MAJOR` if `MINOR` would exceed 9

---

## Unreleased - bounded HTTP input

- Limits every JSON API request to 1 MiB and rejects negative, malformed or
  oversized `Content-Length` values before reading/parsing a request body.
  This prevents an accidental or hostile client from requesting an unbounded
  read on the detector service.

---

## Documentation - Real HTTP API reference

- **`docs/API.md`** (new) - every real endpoint (`POST /baseline/fit`,
  `POST /detect`, `GET /stats`) documented from the actual handler code in
  `api.py`: request/response bodies, status codes, and what the response
  fields (`score`, `anomalous`, `worstBinFreqHz`) actually mean per
  `detector.py`. Verified live against a real running server, not just
  read from source. Documentation-only - no code changed, no version bump.

## [0.1.0]

- **`--addr` now defaults to `127.0.0.1`, not `0.0.0.0`** - found by an
  ecosystem-wide bug audit: this server has no authentication on any
  endpoint (`POST /baseline/fit` lets anyone reachable overwrite the
  statistical baseline this whole detector compares real readings
  against), and the old default bound to every interface. The real CM5's
  own systemd unit already passed `--addr 127.0.0.1` explicitly, matching
  every other internal-only API here (Datalake, Job-Dispatcher,
  Telemetry-Collector) - no real deployment's behavior changes, but
  running this tool bare (no systemd unit, a developer testing it
  locally) is now safe by default instead of silently wide open.
  `docs/API.md` updated to match, with an explicit warning on
  `--addr 0.0.0.0`.

## [0.0.9] - Reject non-finite scores and window samples before they poison a verdict

- **`fft.py`'s `compute_spectrum()`** now rejects a window containing NaN
  or Infinity before running the FFT. Python's `json.loads` accepts the
  non-standard `NaN`/`Infinity`/`-Infinity` tokens by default, so a
  client (or a corrupted telemetry replay) could put one straight into
  `POST /detect`'s `"window"` without ever hitting a JSON parse error.
  A single non-finite sample propagates into *every* bin of `rfft`'s
  output, and any comparison against a NaN score is `False` - the
  detector would have silently reported "not anomalous" instead of
  failing.
- **`drift.py`'s `DriftMonitor`** now rejects a non-finite baseline
  score, a non-finite `drift_ratio_threshold`, and a non-finite `score`
  passed to `observe()`. NaN/Infinity compare `False` against everything
  (including `<= 0`), so a poisoned baseline previously sailed straight
  past the "must be positive" guard and left every future
  `recent_mean / baseline_mean` permanently NaN - drift detection
  silently, permanently disabled by one bad sample rather than failing
  loudly on it.
- **`api.py`'s `_handle_drift_observe`** now catches the `DriftMonitorError`
  `observe()` can raise (it never used to raise at all) and returns a
  clean `400`, instead of the exception reaching an uncaught state in the
  request-handling thread.
- Verified with 6 new tests across `test_fft.py`, `test_drift.py` and a
  real end-to-end `test_api.py` round-trip (`json.dumps`/`json.loads`
  both pass the `NaN`/`Infinity` tokens through on this stdlib pairing,
  so the API-level tests exercise the actual wire behavior, not a
  simulation of it) - `pytest` (55 passed) and `tools/ci_validate.py`.

## [0.0.8] - Fixed a real version-mirror drift

- **`src/hydra_umc_anomaly_detector/__init__.py`**'s `__version__` had
  fallen one real build behind `pyproject.toml`/the manifest - a
  packaging tool that reads only `bump_manifest_version.py`'s own
  `native_version.file` (pyproject.toml) without also running this
  repo's separate `bump_version.py` (the one that keeps `__init__.py`
  mirrored) leaves the two drifting apart, exactly what happened here.
  Fixed via the real, intended sequence (`bump_version.py` then
  `bump_manifest_version.py --sync`), not a manual edit.

## [0.0.7] - Real ecosystem live-status opt-in

- **`hydra-umc.project.json`** declares its real `service.port` (8097)
  and `health_path` (`/stats`) - HYDRA-UMC-SERVER's ecosystem status
  endpoint now does a real HTTP GET against it (expecting 2xx) instead
  of only reporting static manifest metadata.

## [0.0.6] - Fixed a real unhandled-exception crash on malformed `/baseline/fit` and `/detect` requests

- **`src/hydra_umc_anomaly_detector/api.py`** - found in a live ecosystem bug
  audit: `_handle_fit()`'s and `_handle_detect()`'s inner `try`/`except`
  around the actual `detector.fit()`/`detector.score()` calls only caught
  `BaselineError` (plus `NotFittedError` in detect), but both real calls
  route through `fft.compute_spectrum()`, which raises a plain `ValueError`
  - not a `BaselineError` - for a window that is too short (fewer than 2
  samples), not 1-D, or non-numeric. A client `POST` like
  `{"window": [5]}` or `{"window": ["a", "b"]}` crashed the request thread
  with an unhandled exception instead of getting a clean 400. Both inner
  handlers now also catch `ValueError` and return the same clean 400 body
  the outer request-parsing errors already produce.
- 4 new regression tests in `tests/test_api.py` - real malformed HTTP
  bodies (a too-short window, a non-numeric window) sent to both
  `POST /baseline/fit` and `POST /detect`, asserting a clean 400 in every
  case rather than a crash. Full suite: 41 passed, 0 failed.

## [0.0.5] - Model versioning, real precision/recall metrics, simulated drift detection

- **Real model versioning** (`Verdict.model_version`/`threshold`, `AnomalyDetector.model_version`) - every real `fit()` call increments a monotonic version (0 = never fitted), and every `Verdict` now carries both the exact model version and threshold it was scored against - a caller can tell "this verdict came from the baseline fit before the motor was serviced" from "after". `POST /detect` includes both as new, additive response fields.
- **`metrics.py`** (new) - real `precision_recall()`/`PrecisionRecall` (precision/recall/F1 from a real confusion matrix). Turns this project's own prior prose claim ("healthy scored up to ~5.3, faulty scored in the hundreds") into a real, computed number: a new test proves 1.0 precision and 1.0 recall over the exact same synthetic healthy/faulty fixture `test_detector.py` already uses.
- **`drift.py`/`DriftMonitor`** (new) - a real, separate question from "is THIS window anomalous": has the RECENT rolling mean of scores drifted away from the baseline's own known-healthy mean - the kind of slow degradation (a loosening mount, a wearing bearing) that might never spike any single window above the anomaly threshold. New, additive `POST /drift/init`/`POST /drift/observe` endpoints. A real simulated-drift test ramps a fault component up gradually across 40 windows and proves the monitor stays quiet during real healthy priming and the earliest, lightest part of the ramp, then correctly flags the sustained trend once it's genuinely there - and honestly documents a real finding from running the simulation: this detector's own max-z-score design makes a *single* window's own anomaly flag trip earlier than the rolling drift signal for this fault shape, so `DriftMonitor`'s real value here is a robust, sustained-trend confirmation, not a leading indicator.
- 18 new tests (`test_metrics.py`, `test_drift.py` new, plus additions to `test_detector.py`/`test_api.py`) = 37 total. Shared synthetic signal generators were factored out into `tests/signal_fixtures.py` so `test_detector.py`, `test_metrics.py` and `test_drift.py` all test against the exact same real healthy/faulty fixture.
- Real verification beyond the test suite: ran a real `DetectorServer` end-to-end - fit, scored a healthy and a faulty window (confirming `modelVersion`/`threshold` on both), initialized a drift monitor and confirmed it stays quiet on real healthy scores then flags real elevated ones.

## [0.0.4]

- Build version synchronized with `hydra-umc.project.json` and the repository-native version source.

## [0.0.3]

- Build version synchronized with `hydra-umc.project.json` and the repository-native version source.

## [0.0.2] - Real FFT + statistical anomaly detection + HTTP API

- **`src/hydra_umc_anomaly_detector/fft.py`** - real FFT-based spectrum
  computation (`numpy.fft.rfft`), DC bin genuinely dropped. Verified
  against a known synthetic sine wave - the detected peak frequency
  matches the signal's actual frequency, not just "runs without error".
- **`src/hydra_umc_anomaly_detector/baseline.py`** - `fit_baseline()`
  builds a real per-frequency-bin statistical profile (mean/std) from
  known-healthy signal windows, with a `min_std` floor that prevents a
  real division-by-zero when a bin happens to be constant across every
  training window.
- **`src/hydra_umc_anomaly_detector/detector.py`** - `AnomalyDetector`:
  fits a `Baseline`, scores a live window by its worst (max) per-bin
  z-score. Honest naming in the code's own docstring: this is real
  classical signal-processing/statistics, not a trained neural network -
  the README says "AI-driven"; what's actually running today is FFT +
  z-score against a learned baseline. The default threshold (10.0) was
  set from real empirical separation, not guessed - see the module's own
  docstring for the numbers from this project's synthetic
  healthy-vs-faulty test fixtures (an earlier default of 4.0 was found,
  via the tests, to false-positive on genuinely healthy signals because
  a max-over-~250-bins statistic combined with a modest training-set
  size naturally runs higher than a single-bin "4 sigma" intuition
  suggests).
- **`src/hydra_umc_anomaly_detector/api.py`** - plain JSON/HTTP surface
  (stdlib `http.server`): `POST /baseline/fit`, `POST /detect`,
  `GET /stats`. Guards the shared `AnomalyDetector` with a real
  `threading.Lock` (learned from the same cross-thread issue found and
  fixed in HYDRA-UMC-DATALAKE's `TimeSeriesStore` this same session).
- **`src/hydra_umc_anomaly_detector/main.py`** - now wires the detector
  to the API and starts a real HTTP server, instead of only printing
  identity and exiting.
- Added `numpy` as the package's first real runtime dependency, and
  `pytest` as a real dev dependency - `build.sh`/`build.bat` now install
  both and run the real test suite as part of a normal build.
- Verified for real: 19 `pytest` cases across fft/baseline/detector/api,
  including the two that matter most for this project's actual promise -
  a synthetic signal carrying a real extra harmonic (standing in for a
  bearing-defect frequency) is flagged anomalous, a genuinely
  healthy-looking signal is not, with a wide, empirically-measured
  separation margin (healthy scores up to ~5.3, faulty scores in the
  hundreds, at just 10 training windows). Additionally smoke-tested the
  installed CLI entry point end-to-end: real `curl` requests fitting a
  baseline then detecting both a healthy and a faulty signal, correctly
  classified, with the faulty case's `worstBinFreqHz` landing within 1 Hz
  of the actual injected fault frequency.
- What's still not real, on purpose - see `mejoras_futuras.txt`: a
  trained ML/deep-learning model (this is classical FFT + statistics, a
  real foundation for one, not one itself), RUL (Remaining Useful Life)
  estimation, and reading real telemetry from HYDRA-UMC-DATALAKE (this
  detector takes signal windows directly via its API today, not a live
  query against stored history).

## [0.0.1] - Initial scaffolding

- **`src/hydra_umc_anomaly_detector/main.py`** - minimal real entry point. No detection logic yet - statistical/ML-based anomaly detection over HYDRA-UMC-DATALAKE's own telemetry lands in a later pass.
- **`pyproject.toml`** - packaging metadata, no runtime dependencies yet.
- **`bump_version.py`** - ecosystem-standard odometer bump script.
- **`build.sh` / `build.bat`**, **`run.sh` / `run.bat`** - venv creation, editable install, compile-check, and entry-point execution.
