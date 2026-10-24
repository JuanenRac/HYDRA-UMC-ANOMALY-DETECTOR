# Changelog

All notable changes to this project are documented here.

## [Unreleased]

- **One detector per series.** `POST /detectors/{sourceId}/{kind}/{field}/fit`, `.../detect`, `.../drift/init`, `.../drift/observe` and `GET /detectors` give each series its own baseline, model version, drift monitor and lock, because a motor and a spindle do not vibrate alike and one shared baseline called the other anomalous. A series is created by its first successful fit (a failed fit takes no place), at most `--max-series` (default 64) exist, scoring a series that was never fitted is `409` and never a verdict from another baseline, and fitting a series never touches another or the shared one. Writing needs the token, scoring only with `--protect-reads`. `--baseline-dir` saves one baseline per series (named by a digest of the key, so a name never becomes a path) and restores them at start. The original routes are unchanged. `AnomalyDetector.sample_rate` is now a property. 23 new tests (112 in all).
- **Reads can be protected too.** `--protect-reads` (or `HYDRA_UMC_ANOMALY_PROTECT_READS=1`, needs a token) makes `POST /detect` demand the Bearer token as the writing routes do; only `GET /stats` stays open. Off by default.
- **An optional Hann window** (`--window hann`; the default `none` is the original behaviour): the mean is removed, a periodic Hann window is applied before the FFT and the magnitudes are scaled so a sine of amplitude A peaks near A. A frequency between two bins leaks far less. The numbers differ, so a baseline remembers the window it was made with (older files load as `none`), a detector refuses to restore a baseline made with another window, `GET /stats` reports the window, and the threshold has to be recalibrated with real healthy recordings before Hann is trusted.
- **The training history from DATALAKE is looked at, not only read**: `fetch_windows_checked()` pages the read (DATALAKE answers the oldest points first, up to its limit), removes repeated timestamps, cuts the series wherever two neighbours are not about `1/sample-rate` apart and makes windows inside each uninterrupted run only; it says how many points, pages, repeats, cuts and dropped samples there were, and when it stopped at `--max-train-points`. New `--train-start`, `--train-end` and `--max-train-points`.
- **A bearer token for the writing routes**: `--token-file` (or `HYDRA_UMC_ANOMALY_TOKEN`, at least 16 characters) makes `POST /baseline/fit`, `/drift/init` and `/drift/observe` demand it; `/detect` and `GET /stats` stay open. A non-loopback `--addr` is refused without a token.
- 14 new tests (87 in all).
- Building no longer changes the version; raising it is the explicit `bump` command (`python tools/hydra_project_tool.py bump`). The build scripts only report the version they built, and CI fails if a build script runs a bump.
- The build and run scripts use the Python of the `.venv` directly instead of an activate script, and create the venv with the `py` launcher when there is one; CI fails if a script activates a venv.
- The banner follows the animated style of the ecosystem banners, with the name, the kind and the technologies of the project.
- Added `tools/hydra_project_tool.py`: the same `validate`, `test`, `version`, `status` and `bump` commands in every repository. The checks run with `HYDRA_UMC_CI=1` and fail if the version changes; only `bump` changes it.

## [0.1.7]

- Maturity is functional: nothing here has been verified on real hardware yet.
- The READMEs say, in the eight languages, what is planned and what has only been tested against fakes, emulators or fixtures, and carry a short safety and security status note.

## [0.1.6]

- Build version synchronized with `hydra-umc.project.json` and the repository-native version source.
- The licence is written as GPL-3.0-or-later in every header and in the package metadata.

## [0.1.5] - Bad input gets an answer, a baseline stays tied to its sample rate, a writable state directory

- **HTTP service:** a window with non-numeric elements (`{"windows": [{"a": 1}]}`) or an infinite `windowSize` made the handler raise and the server closed the connection without any reply; both are now a clean 400, and `NaN`/`Infinity` constants in a request body are refused. A refit no longer blocks scoring while its FFTs run (the heavy work happens before taking the lock).
- **Drift monitor:** it is tied to the model it was initialized for; after a refit `/drift/observe` answers 409 until `/drift/init` is called again, instead of comparing new scores with the previous model's reference.
- **Sample rate:** restoring a saved baseline under a different `--sample-rate` is refused with a clear message (it used to compare bins that do not correspond and report a false frequency).
- **Persistence:** a failure to save the fitted baseline at startup is a one-line error instead of a traceback, and the systemd unit gets `StateDirectory=hydra-umc-anomaly-detector` and passes `--baseline-path /var/lib/hydra-umc-anomaly-detector/baseline.npz`, because `ProtectSystem=strict` made the path in the README unwritable.
- **Metrics:** `false_positive_rate` and `specificity` next to precision, which is perfect for a detector that never raises an alarm.
- **Docs:** the threshold docstring now says it was checked only on fixtures with fixed amplitude, phase and bin-exact frequency and must be recalibrated on real recordings; the API reference lists `calibrationWindows`, the 500 on a failed save and the new rules; the changelog no longer claims the version script carries into MAJOR; the README uses the writable baseline path (73 tests).

## [0.1.4] - Durable baseline file, idle connections dropped, Portuguese README

- **Durable baseline file:** `--baseline-path` wrote the fitted baseline in place, so a power cut or a full disk in the middle of the write (a CM5 is power-cycled) left a truncated file that `load_baseline()` refused, and the last good baseline was lost with it. The file is now written next to its target, flushed and fsynced, then renamed over it (and the directory synced where the OS allows), so the previous baseline stays intact until the new one is complete; a failed write removes its temporary file and leaves the old one untouched.
- **Idle connections are dropped:** the HTTP handler had no connection timeout, so a client that opened a connection and sent nothing (or sent a body one byte at a time) held a handler thread forever. A silent client is now dropped after 15 seconds; the limit covers socket I/O only, never the time a request spends computing.
- **Tests:** new tests cover the atomic save (no temporary file left, replacing an existing file, a write that fails midway keeps the previous baseline) and the dropped idle connection, 67 in total.
- **Docs:** the README test count is corrected and a README in Portuguese (`README_por.md`) joins the other translations.

## [0.1.3]

- **Calibration provenance:** a fitted `Baseline` now records how many
  healthy windows and which sample rate it was calibrated from. The
  numbers are persisted with the baseline (files saved before this load
  as "unknown", never guessed), carried on every `Verdict`
  (`calibration_windows`) and returned by `POST /detect` as
  `calibrationWindows`, so an alarm can be explained and reproduced.
- README versioning bullet updated in all seven languages.

## [0.1.2] - Real DATALAKE client, persisted baseline, and a public-doc fix

- **Fixed a stale doc reference:**
  `detector.py` (two sites) and this file pointed a reader at a tracking
  file that doesn't exist anywhere in this repo - a stale/broken
  reference, not a link to a real document. Replaced with the same
  explanation inline: a trained ML model needs real labeled field data
  to train and validate against, which doesn't exist yet, so that stays
  deliberately deferred future work.
- **New `datalake_client.py`** (`DatalakeClient`, `fetch_windows()`) -
  this project's own README describes running "on telemetry
  HYDRA-UMC-TELEMETRY-COLLECTOR already wrote there", but no code
  anywhere in this repo ever actually queried HYDRA-UMC-DATALAKE - today
  that meant pasting raw arrays into `POST /baseline/fit`/`POST /detect`
  by hand. Same shape (and the same transient-network retry already
  landed this session) as HYDRA-UMC-PRODUCTION-REPORTS' own
  `datalake_client.py`. `fetch_windows()` chunks DATALAKE's real,
  timestamp-ordered points into the fixed-length windows
  `fit_baseline()`/`AnomalyDetector.score()` require, dropping a
  trailing partial window rather than crashing or silently comparing
  mismatched bin counts. New `--datalake-url`/`--source-id`/`--kind`/
  `--field`/`--window-size` flags fit a fresh baseline from real DATALAKE
  history at startup.
- **The fitted baseline can now survive a restart** (new
  `save_baseline()`/`load_baseline()` in `baseline.py`,
  `AnomalyDetector.restore()`) - the fitted
  baseline lived only in memory, forcing a re-run of
  `POST /baseline/fit` by hand after every service restart. New
  `--baseline-path` flag persists to a real `.npz` file on every
  successful fit and restores from it on startup (skipped if
  `--datalake-url` already fit a fresh one this run). `restore()`
  deliberately does not bump `model_version` - it's the same real model
  that already existed before the restart, not a new one. Real
  end-to-end tests prove both the CLI round trip and a live server
  persisting on a real `POST /baseline/fit`, not just the underlying
  save/load functions in isolation.
- **`.github/workflows/ci.yml`** - the real `tests/` pytest suite is now
  actually installed and run in CI, matching the fix already landed in
  sibling repos this session. This repo's own baseline workflow only ever
  compile-checked (`py_compile`) `.py` files - it never ran `pytest`, so a
  regression in `tests/` could be merged without CI ever failing. CI-only
  fix, no runtime code changed, no version bump.

## [0.1.1]

- **Fixed a real, intermittent connection reset on oversized requests.**
  Rejecting a request over the 1 MiB
  limit closed the connection without ever reading any of the declared
  body; once that body was larger than the OS socket buffer, the client's
  own in-flight write got cut off and it saw a raw `ConnectionAbortedError`
  instead of the intended clean `400`. Flaky by nature - it depended on how
  much the kernel had already buffered before the handler responded. Fixed
  by draining a bounded amount of the oversized body first, so the client
  always finishes sending before the response goes out.
- Build version synchronized with `hydra-umc.project.json` and the repository-native version source.

**Bounded HTTP input**

- Limits every JSON API request to 1 MiB and rejects negative, malformed or
  oversized `Content-Length` values before reading/parsing a request body.
  This prevents an accidental or hostile client from requesting an unbounded
  read on the detector service.

## [0.1.0]

- **`--addr` now defaults to `127.0.0.1`, not `0.0.0.0`** - this server
  has no authentication on any
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

- **`src/hydra_umc_anomaly_detector/api.py`** - `_handle_fit()`'s and
  `_handle_detect()`'s inner `try`/`except`
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
- What's still not real, on purpose, and deliberately deferred future
  work: a trained ML/deep-learning model (this is classical FFT +
  statistics, a real foundation for one, not one itself), RUL (Remaining
  Useful Life) estimation, and reading real telemetry from
  HYDRA-UMC-DATALAKE (this detector takes signal windows directly via
  its API today, not a live query against stored history).

## [0.0.1] - Initial scaffolding

- **`src/hydra_umc_anomaly_detector/main.py`** - minimal real entry point. No detection logic yet - statistical/ML-based anomaly detection over HYDRA-UMC-DATALAKE's own telemetry lands in a later pass.
- **`pyproject.toml`** - packaging metadata, no runtime dependencies yet.
- **`bump_version.py`** - ecosystem-standard odometer bump script.
- **`build.sh` / `build.bat`**, **`run.sh` / `run.bat`** - venv creation, editable install, compile-check, and entry-point execution.

## Earlier history

### Documentation - Real HTTP API reference

- **`docs/API.md`** (new) - every real endpoint (`POST /baseline/fit`,
  `POST /detect`, `GET /stats`) documented from the actual handler code in
  `api.py`: request/response bodies, status codes, and what the response
  fields (`score`, `anomalous`, `worstBinFreqHz`) actually mean per
  `detector.py`. Verified live against a real running server, not just
  read from source. Documentation-only - no code changed, no version bump.
