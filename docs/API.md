# HTTP API Reference

Real, plain JSON/HTTP surface implemented in
[`src/hydra_umc_anomaly_detector/api.py`](../src/hydra_umc_anomaly_detector/api.py)
with Python's stdlib `http.server` (no framework dependency). One
`AnomalyDetector` instance lives for the process's lifetime, guarded by a
lock so concurrent requests can't corrupt its numpy state.

Start it with:

```bash
hydra-umc-anomaly-detector --addr 127.0.0.1 --port 8097
```

`--addr`/`--port` default to `127.0.0.1:8097` - loopback-only, matching every other internal API in this ecosystem (Datalake, Job-Dispatcher, Telemetry-Collector) and the real CM5's own systemd unit, which already passed this same override explicitly. Run `hydra-umc-anomaly-detector --help` for the full flag list (including `--sample-rate` and `--threshold`, which configure the `AnomalyDetector` the server wraps).

All responses are `application/json`. Without a token there is no authentication on any endpoint, which is why a non-loopback `--addr` is refused unless a token is set (`--token-file` or `HYDRA_UMC_ANOMALY_TOKEN`, at least 16 characters): with one, every route but `GET /stats` demands `Authorization: Bearer <token>` (reads too, unless `--no-protect-reads`). `POST /baseline/fit` would otherwise let anyone reachable overwrite the statistical baseline this detector compares real readings against.

Every JSON request body is limited to 1 MiB. Missing, negative, malformed or
oversized `Content-Length` values are rejected with `400` before JSON parsing.

---

## `POST /baseline/fit`

Fits the detector's statistical baseline from known-healthy signal windows. Must be called at least once before `/detect` will accept requests.

**Request body**

```json
{
  "windows": [[0.01, 0.02, -0.01, ...], [0.02, 0.01, 0.0, ...]]
}
```

`windows` is a non-empty array of arrays - each inner array is one healthy time-domain signal window (floats), sampled at the server's configured `sample_rate`. At least 10 windows are needed (the baseline uses the sample standard deviation); the server started with `--legacy-baseline` accepts 2 and uses the population one. `--min-windows`, `--ddof` and the other tuned values can also be set in the JSON file named by `--detector-config`.

**Responses**

| Status | Body | Meaning |
|---|---|---|
| 200 | `{"status": "fitted", "windowCount": <int>}` | Baseline fitted from `windowCount` windows. |
| 400 | `{"error": "<message>"}` | `windows` missing/empty/not an array, or the baseline computation itself rejected the input (e.g. inconsistent window lengths) - see `BaselineError` in [`baseline.py`](../src/hydra_umc_anomaly_detector/baseline.py). Non-numeric values inside a window, and `NaN`/`Infinity` constants, are also a 400. |
| 500 | `{"error": "fitted but could not persist baseline: <reason>"}` | The baseline is fitted and usable in memory, but saving it to `--baseline-path` failed. |

Calling this again replaces the previous baseline - it is not additive. A drift monitor initialized before the refit is refused by `/drift/observe` (409) until `/drift/init` is called again, because its reference scores belong to the old baseline. A baseline restored from `--baseline-path` must have been fitted at the same `--sample-rate`.

---

## One detector per series

The routes above keep ONE shared baseline. A motor and a spindle do not vibrate alike, and a baseline fitted on one calls the other anomalous, so every operation also exists for a single series, named by the triple HYDRA-UMC-DATALAKE stores it under:

| Route | Same as | Needs the token (when one is set) |
|---|---|---|
| `POST /detectors/{sourceId}/{kind}/{field}/fit` | `POST /baseline/fit` | yes |
| `POST /detectors/{sourceId}/{kind}/{field}/detect` | `POST /detect` | unless `--no-protect-reads` |
| `POST /detectors/{sourceId}/{kind}/{field}/drift/init` | `POST /drift/init` | yes |
| `POST /detectors/{sourceId}/{kind}/{field}/drift/observe` | `POST /drift/observe` | yes |
| `GET /detectors` | - | unless `--no-protect-reads` |

`GET /detectors` answers `{"detectors": [{"sourceId", "kind", "field", "fitted", "modelVersion"}, ...]}`. Each of the three names is 1 to 96 characters of letters, digits, dot, dash, colon or underscore, starting with a letter or a digit (anything else is a `400`); the names are URL-decoded.

A series is created by its first successful fit. A fit that fails (`400`) takes no place; when `--max-series` (default 64) series already exist a new one is refused with `409`, and an existing one can always be refitted. Scoring a series that has no baseline is `409` ("fit it first"), never a verdict from another baseline. Each series has its own model version, its own drift monitor (a refit of one series invalidates only its own monitor) and its own lock, so working on one never waits for another. With `--baseline-dir DIR` each series saves its baseline (`<digest>.npz` and `<digest>.key.json`, the digest of the key: a name never becomes a path) and they are restored at start; one that cannot be restored is reported and skipped.

---

## `POST /detect`

Scores one live signal window against the fitted baseline.

**Request body**

```json
{
  "window": [0.01, 0.02, -0.01, ...]
}
```

`window` is a non-empty array of floats, one time-domain signal window at the server's configured `sample_rate`.

**Responses**

| Status | Body | Meaning |
|---|---|---|
| 200 | `{"score": <float>, "anomalous": <bool>, "worstBinFreqHz": <float>, "modelVersion": <int>, "threshold": <float>, "calibrationWindows": <int>}` | See below. |
| 400 | `{"error": "<message>"}` | `window` missing/empty/not an array, or malformed relative to the fitted baseline. |
| 409 | `{"error": "call fit() with known-healthy windows before score()"}` | `/baseline/fit` has never been called successfully. |

**Response fields** (real computation, in [`detector.py`](../src/hydra_umc_anomaly_detector/detector.py)):

- `score` - the worst (max absolute) per-frequency-bin z-score across the window's FFT spectrum, relative to the fitted baseline's mean/std per bin. A real fault tends to show up as an outlier in a few bins, not a uniform shift, so the *worst* bin - not an average - drives the verdict.
- `anomalous` - `true` when `score` exceeds the detector's configured threshold (default `10.0` - see the constructor docstring in `detector.py` for how that default was empirically chosen).
- `worstBinFreqHz` - the real FFT bin frequency (Hz) that produced `score`, i.e. which frequency is behaving anomalously.
- `calibrationWindows` - how many healthy windows the baseline was fitted from (0 for a baseline saved before this was recorded).
- `modelVersion` - the real, monotonic version of the baseline this verdict was scored against (incremented on every real `/baseline/fit` call).
- `threshold` - the real threshold this verdict was compared against.

---

## `GET /stats`

**Response**

| Status | Body | Meaning |
|---|---|---|
| 200 | `{"fitted": <bool>}` | Whether `/baseline/fit` has been called successfully at least once. |

---

## `POST /drift/init`

Creates (or replaces) the server's `DriftMonitor`, seeded from a real set of reference "known-healthy" scores - typically `Verdict.score` values from scoring known-healthy windows right after `/baseline/fit`.

**Request body**

```json
{"baselineScores": [4.1, 5.0, 4.8], "windowSize": 20, "driftRatioThreshold": 2.0}
```

`baselineScores` (required, non-empty array of numbers) is the real reference. `windowSize` (optional, default `20`) is how many recent scores the rolling mean covers. `driftRatioThreshold` (optional, default `2.0`, must be `> 1.0`) is how many times the baseline mean the recent rolling mean must reach to count as drift.

**Responses**

| Status | Body | Meaning |
|---|---|---|
| 200 | `{"status": "initialized", "baselineMeanScore": <float>}` | Monitor created. |
| 400 | `{"error": "<message>"}` | Missing/empty `baselineScores`, or an invalid `windowSize`/`driftRatioThreshold`. |

---

## `POST /drift/observe`

Records one real score and reports the current real drift state.

**Request body**

```json
{"score": 5.2}
```

**Responses**

| Status | Body | Meaning |
|---|---|---|
| 200 | `{"status": "priming"}` | Fewer than `windowSize` real scores observed so far - not enough evidence yet, no verdict. |
| 200 | `{"status": "ready", "baselineMeanScore": <float>, "recentMeanScore": <float>, "driftRatio": <float>, "drifted": <bool>}` | The rolling window is full; `driftRatio` is `recentMeanScore / baselineMeanScore`. |
| 400 | `{"error": "<message>"}` | Missing/malformed `score`. |
| 409 | `{"error": "drift monitor not initialized - call POST /drift/init first"}` | `/drift/init` was never called. |

---

## `POST /rul` (off by default: `--rul`)

Days until the trend of a list of scores reaches a limit. Body: `{"points": [{"t": seconds, "score": number}, ...], "limit": number}` (at most 1000 points). The trend is the Theil-Sen line (median of the pairwise slopes), so a few wild points do not move it. Answer: `status` (`insufficient_data` below `--rul-min-points` (or `rulMinPoints` of `--detector-config`; default 8, at least 3, and `"rul": true` there turns the route on like `--rul`); `not_rising`; `limit_reached`; `estimated`), `points`, `slopePerDay`, `currentValue`, `daysToLimit`, and a range `daysLow` (the steepest plausible trend) and `daysHigh` (the gentlest; `null` when that trend does not rise). It is an estimate of a trend, never a promise, and it changes nothing. It needs the token when reads are protected; the route answers 404 while `--rul` (or `"rul": true` in `--detector-config`) is off.

## Continuous service mode (`--follow-config FILE`)

Without it a verdict exists only when somebody posts a window. With it the detector follows the series named in a JSON file: every
`intervalSeconds` it asks DATALAKE for the points after the last one it used, cuts them wherever the spacing is not about `1 / sample-rate`
(a window over a hole describes the hole), and scores every complete window with the detector of that series. A series nobody fitted is
reported once (`note: "not fitted"` in `GET /stats`) and scored as soon as it has a baseline.

```json
{
  "datalake": {"url": "http://127.0.0.1:8095", "readTokenFile": "/etc/hydra-umc/anomaly-read.token", "ingestTokenFile": "/etc/hydra-umc/anomaly-ingest.token"},
  "intervalSeconds": 5,
  "maxWindowsPerTick": 20,
  "writeScores": true,
  "scoreKind": "anomaly_score",
  "lookbackWindows": 2,
  "gapTolerance": 0.5,
  "topicPrefix": "hydra/anomaly",
  "mqtt": {"host": "127.0.0.1", "port": 1883, "clientId": "hydra-umc-anomaly-detector", "tls": false, "caFile": null,
           "usernameFile": null, "passwordFile": null, "allowPlaintext": false, "timeoutSeconds": 5},
  "series": [{"sourceId": "spindle-1", "kind": "vibration", "field": "value", "windowSize": 512}]
}
```

Only `datalake.url` and `series` are required. The DATALAKE tokens come from the files named there, or from `HYDRA_UMC_DATALAKE_READ_TOKEN` /
`HYDRA_UMC_DATALAKE_INGEST_TOKEN` / `HYDRA_UMC_DATALAKE_TOKEN` (each also as `..._FILE`). The detector's own `--sample-rate` is the rate of every series.

* **The verdict is written back** (`writeScores`) as a sample of kind `scoreKind` for the same `sourceId`, timestamped with the last sample of the window,
  with the fields `<field>` (the score), `<field>_anomalous` (0 or 1) and `<field>_model` (the model version). A failed write is counted and does not
  lose the verdict or stop the loop.
* **MQTT** (when `mqtt` is present; QoS 0, a connection per message): a series that changes state publishes a *retained* JSON message to
  `<topicPrefix>/<sourceId>/<kind>/<field>/state` (the first verdict counts as a change), and every anomalous window publishes one to
  `.../alarm` (not retained). The message holds `sourceId`, `kind`, `field`, `timestamp`, `score`, `threshold`, `anomalous` and `modelVersion`.
  A broker that is not on this machine needs `tls`, or `allowPlaintext` to say on purpose that plain text is fine; the user name and password
  are read from files.
* **`GET /stats`** gains a `follower` object: passes, windows scored, anomalies, DATALAKE / write / MQTT errors, and per series the last score,
  state and note. A DATALAKE or broker that cannot be reached is counted and tried again at the next pass.

---

## Errors

Any other path/method returns `404 {"error": "not found"}`.
