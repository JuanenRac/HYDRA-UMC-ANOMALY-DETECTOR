# HTTP API Reference

Real, plain JSON/HTTP surface implemented in
[`src/hydra_umc_anomaly_detector/api.py`](../src/hydra_umc_anomaly_detector/api.py)
with Python's stdlib `http.server` (no framework dependency). One
`AnomalyDetector` instance lives for the process's lifetime, guarded by a
lock so concurrent requests can't corrupt its numpy state.

Start it with:

```bash
hydra-umc-anomaly-detector --addr 0.0.0.0 --port 8097
```

`--addr`/`--port` default to `0.0.0.0:8097`. Run `hydra-umc-anomaly-detector --help` for the full flag list (including `--sample-rate` and `--threshold`, which configure the `AnomalyDetector` the server wraps).

All responses are `application/json`. There is no authentication - this is an internal, same-host/same-network service, not exposed publicly.

---

## `POST /baseline/fit`

Fits the detector's statistical baseline from known-healthy signal windows. Must be called at least once before `/detect` will accept requests.

**Request body**

```json
{
  "windows": [[0.01, 0.02, -0.01, ...], [0.02, 0.01, 0.0, ...]]
}
```

`windows` is a non-empty array of arrays - each inner array is one healthy time-domain signal window (floats), sampled at the server's configured `sample_rate`.

**Responses**

| Status | Body | Meaning |
|---|---|---|
| 200 | `{"status": "fitted", "windowCount": <int>}` | Baseline fitted from `windowCount` windows. |
| 400 | `{"error": "<message>"}` | `windows` missing/empty/not an array, or the baseline computation itself rejected the input (e.g. inconsistent window lengths) - see `BaselineError` in [`baseline.py`](../src/hydra_umc_anomaly_detector/baseline.py). |

Calling this again replaces the previous baseline - it is not additive.

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
| 200 | `{"score": <float>, "anomalous": <bool>, "worstBinFreqHz": <float>}` | See below. |
| 400 | `{"error": "<message>"}` | `window` missing/empty/not an array, or malformed relative to the fitted baseline. |
| 409 | `{"error": "call fit() with known-healthy windows before score()"}` | `/baseline/fit` has never been called successfully. |

**Response fields** (real computation, in [`detector.py`](../src/hydra_umc_anomaly_detector/detector.py)):

- `score` - the worst (max absolute) per-frequency-bin z-score across the window's FFT spectrum, relative to the fitted baseline's mean/std per bin. A real fault tends to show up as an outlier in a few bins, not a uniform shift, so the *worst* bin - not an average - drives the verdict.
- `anomalous` - `true` when `score` exceeds the detector's configured threshold (default `10.0` - see the constructor docstring in `detector.py` for how that default was empirically chosen).
- `worstBinFreqHz` - the real FFT bin frequency (Hz) that produced `score`, i.e. which frequency is behaving anomalously.

---

## `GET /stats`

**Response**

| Status | Body | Meaning |
|---|---|---|
| 200 | `{"fitted": <bool>}` | Whether `/baseline/fit` has been called successfully at least once. |

---

## Errors

Any other path/method returns `404 {"error": "not found"}`.
