# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - src/hydra_umc_anomaly_detector/main.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Entry point for HYDRA-UMC-ANOMALY-DETECTOR.

Real FFT + statistical anomaly detection, no longer just an identity
print: fft.py computes a real spectrum (numpy), baseline.py learns a
real per-frequency-bin healthy profile, detector.py scores live windows
against it, api.py exposes POST /baseline/fit + POST /detect + GET
/stats.

Honest naming, see detector.py's own docstring: this is real classical
signal-processing/statistics, not a trained neural network - the README
says "AI-driven"; what's actually running today is FFT + z-score against
a learned baseline, a real and legitimate technique, just not deep
learning yet.
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import sys
from pathlib import Path

from . import __version__
from .api import DetectorServer
from .baseline import BaselineError, load_baseline, save_baseline
from .datalake_client import DatalakeClient, DatalakeError, fetch_windows_checked
from .follower import Follower, FollowConfigError, load_follow_config
from .mqtt_publish import MqttPublisher
from .fft import WINDOWS
from .detector import AnomalyDetector
from .registry import DEFAULT_MAX_DETECTORS, DetectorRegistry

PROJECT_NAME = "HYDRA-UMC-ANOMALY-DETECTOR"
ROLE = (
    "Anomaly-Detector - AI-driven predictive maintenance, FFT/spectrogram "
    "analysis of motor vibration signatures fed by HYDRA-UMC-DATALAKE."
)


def _secret(raw: dict, file_key: str, env_names: tuple[str, ...]) -> str | None:
    """A token from the file the configuration names, else from the first environment variable that is set (or its _FILE twin)."""
    path = raw.get(file_key)
    if path is not None:
        if not isinstance(path, str) or not path:
            raise FollowConfigError(f"datalake.{file_key} must be the path of a file")
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as e:
            raise FollowConfigError(f"cannot read datalake.{file_key}: {e}") from e
    for name in env_names:
        if os.environ.get(name):
            return os.environ[name].strip()
        file_name = os.environ.get(name + "_FILE")
        if file_name:
            try:
                return Path(file_name).read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError) as e:
                raise FollowConfigError(f"cannot read the file named by {name}_FILE: {e}") from e
    return None


def _follow_client(config, raw: dict) -> DatalakeClient:
    section = raw.get("datalake", {})
    read_token = _secret(section, "readTokenFile", ("HYDRA_UMC_DATALAKE_READ_TOKEN", "HYDRA_UMC_DATALAKE_TOKEN"))
    ingest_token = _secret(section, "ingestTokenFile", ("HYDRA_UMC_DATALAKE_INGEST_TOKEN", "HYDRA_UMC_DATALAKE_TOKEN"))
    return DatalakeClient(config.datalake_url, read_token=read_token, ingest_token=ingest_token)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hydra-umc-anomaly-detector")
    # Real gap: this used to default to
    # "0.0.0.0" (every interface) with zero authentication on any
    # endpoint (POST /baseline/fit lets anyone reachable overwrite the
    # statistical baseline this whole detector compares real readings
    # against) - the real CM5's own systemd unit already overrides this
    # to "127.0.0.1" explicitly, matching every other internal-only API
    # here (Datalake, Job-Dispatcher, Telemetry-Collector), so making it
    # the real default too means running this tool bare (no systemd
    # unit, a developer testing it locally) is safe by default instead
    # of silently wide open.
    parser.add_argument("--addr", default="127.0.0.1", help="address to bind the HTTP API to")
    parser.add_argument("--port", type=int, default=8097, help="port for the HTTP API")
    parser.add_argument(
        "--sample-rate",
        type=float,
        default=1000.0,
        help="sample rate (Hz) every submitted signal window is assumed to use",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=10.0,
        help="max per-bin z-score above which a reading is flagged anomalous (see detector.py's own docstring for why 10.0)",
    )
    # Real gap - the fitted baseline lived only in memory - a service restart lost
    # it and forced re-running POST /baseline/fit by hand. All optional;
    # omitted (the default) means every existing behavior is unchanged.
    parser.add_argument(
        "--baseline-path",
        default=None,
        help="Persist the fitted baseline here (numpy .npz) and restore it from here on startup if present.",
    )
    # Real gap: this project's own README
    # describes running "on telemetry HYDRA-UMC-TELEMETRY-COLLECTOR
    # already wrote there", but nothing here ever actually queried
    # HYDRA-UMC-DATALAKE - fitting meant pasting raw arrays by hand.
    # Given together with --source-id/--kind/--field/--window-size, this
    # fetches real training windows and fits once at startup (before
    # trying to restore --baseline-path, so a fresh fit always wins over
    # a possibly-stale persisted one when both are given).
    parser.add_argument("--datalake-url", default=None, help="HYDRA-UMC-DATALAKE base URL to fit a fresh baseline from at startup, e.g. http://127.0.0.1:8095")
    parser.add_argument("--source-id", default=None, help="DATALAKE sourceId to fetch training windows from (required with --datalake-url)")
    parser.add_argument("--kind", default=None, help="DATALAKE kind to fetch training windows from (required with --datalake-url)")
    parser.add_argument("--field", default=None, help="DATALAKE field to fetch training windows from (required with --datalake-url)")
    parser.add_argument("--window-size", type=int, default=None, help="samples per training window (required with --datalake-url)")
    parser.add_argument("--legacy-baseline", action="store_true", help="fit baselines the earlier way: 2 healthy windows are enough and the spread is the population standard deviation (default: at least 10 windows and the sample standard deviation, which gives fewer false alarms)")
    parser.add_argument("--window", choices=WINDOWS, default="none", help="window applied before the FFT: none (the original behaviour), hann (removes the mean and the leakage of a speed that wanders) or welch (Hann segments averaged in power: steadier magnitudes, half the frequency resolution); recalibrate --threshold with real healthy recordings before trusting hann or welch. A baseline remembers the window it was made with.")
    parser.add_argument("--train-start", type=int, default=None, help="first timestamp (unix ms) of the training history read from DATALAKE")
    parser.add_argument("--train-end", type=int, default=None, help="last timestamp (unix ms) of the training history read from DATALAKE")
    parser.add_argument("--max-train-points", type=int, default=200_000, help="most points read from DATALAKE for training (the read is paged, oldest first; reaching this is reported)")
    parser.add_argument("--baseline-dir", default=None, help="Persist the baseline of each series (POST /detectors/{sourceId}/{kind}/{field}/fit) in this directory and restore them on startup.")
    parser.add_argument("--max-series", type=int, default=DEFAULT_MAX_DETECTORS, help="most series that may have a detector of their own at once (default %(default)s)")
    parser.add_argument("--token-file", default=None, help="file with a bearer token demanded by POST /baseline/fit, /drift/init and /drift/observe (or the HYDRA_UMC_ANOMALY_TOKEN environment variable). Needed to bind a non-loopback address.")
    parser.add_argument("--protect-reads", dest="protect_reads", action="store_true", default=None, help="POST /detect demands the token too (only GET /stats stays open). This is the default whenever a token is set; HYDRA_UMC_ANOMALY_PROTECT_READS=1 does the same and, unlike the default, refuses to start without a token")
    parser.add_argument("--no-protect-reads", dest="protect_reads", action="store_false", help="leave POST /detect open even though a token is set (HYDRA_UMC_ANOMALY_PROTECT_READS=0 does the same)")
    parser.add_argument("--follow-config", default=None, help="continuous service mode: a JSON file naming the series to read from DATALAKE as they grow, score, write back and raise alarms for (see docs/API.md); every tuned value lives in that file")
    args = parser.parse_args(argv)

    token = os.environ.get("HYDRA_UMC_ANOMALY_TOKEN", "")
    if args.token_file:
        try:
            token = Path(args.token_file).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as e:
            print(f"error: cannot read --token-file: {e}", file=sys.stderr)
            return 2
    if token and len(token) < 16:
        print("error: the bearer token must have at least 16 characters", file=sys.stderr)
        return 2
    try:
        loopback = args.addr == "localhost" or ipaddress.ip_address(args.addr).is_loopback
    except ValueError:
        loopback = False
    if not loopback and not token:
        print(f"error: --addr {args.addr} is not loopback: set --token-file (or HYDRA_UMC_ANOMALY_TOKEN) so the baseline cannot be replaced by anyone who reaches the port", file=sys.stderr)
        return 2

    if args.datalake_url is not None and (args.source_id is None or args.kind is None or args.field is None or args.window_size is None):
        print("error: --datalake-url requires --source-id, --kind, --field and --window-size", file=sys.stderr)
        return 2

    print(f"{PROJECT_NAME} v{__version__}")
    print(ROLE)

    detector = AnomalyDetector(sample_rate=args.sample_rate, threshold=args.threshold, window=args.window, legacy_baseline=args.legacy_baseline)
    baseline_path = Path(args.baseline_path) if args.baseline_path else None

    if args.datalake_url is not None:
        client = DatalakeClient(args.datalake_url)
        try:
            fetched = fetch_windows_checked(
                client, source_id=args.source_id, kind=args.kind, field=args.field, window_size=args.window_size,
                sample_rate=args.sample_rate, start=args.train_start, end=args.train_end, max_points=args.max_train_points,
            )
            windows = fetched.windows
            print(f"[anomaly-detector] read from DATALAKE: {fetched.summary()}")
            if fetched.truncated:
                print("[anomaly-detector] WARNING: the history is longer than --max-train-points; the baseline is made from the oldest part only. Pick the training range with --train-start/--train-end.", file=sys.stderr)
            detector.fit(windows)
        except (DatalakeError, BaselineError, ValueError) as e:
            print(f"error: could not fit from DATALAKE: {e}", file=sys.stderr)
            return 1
        print(f"[anomaly-detector] fit {len(windows)} window(s) from DATALAKE ({args.datalake_url})")
        if baseline_path is not None:
            try:
                save_baseline(baseline_path, detector.baseline, detector.model_version)
            except OSError as e:
                print(f"error: could not save the baseline to {baseline_path}: {e}", file=sys.stderr)
                return 1
            print(f"[anomaly-detector] persisted fresh baseline to {baseline_path}")
    elif baseline_path is not None and baseline_path.is_file():
        try:
            baseline, model_version = load_baseline(baseline_path)
            detector.restore(baseline, model_version)
            print(f"[anomaly-detector] restored baseline from {baseline_path} (model version {model_version})")
        except (BaselineError, ValueError) as e:
            print(f"error: could not restore baseline from {baseline_path}: {e}", file=sys.stderr)
            return 1

    protect_reads = args.protect_reads
    if protect_reads is None:
        env_value = os.environ.get("HYDRA_UMC_ANOMALY_PROTECT_READS", "").strip()
        if env_value == "1":
            protect_reads = True
        elif env_value == "0":
            protect_reads = False
    if protect_reads and not token:
        print("error: --protect-reads needs a token (--token-file or HYDRA_UMC_ANOMALY_TOKEN)", file=sys.stderr)
        return 2
    if protect_reads is None:
        # Default: reads are protected whenever there is a token to demand; without one (loopback only) there is nothing to check.
        protect_reads = bool(token)
    if args.max_series < 1:
        print("error: --max-series must be at least 1", file=sys.stderr)
        return 2
    registry = DetectorRegistry(
        lambda: AnomalyDetector(sample_rate=args.sample_rate, threshold=args.threshold, window=args.window, legacy_baseline=args.legacy_baseline),
        Path(args.baseline_dir) if args.baseline_dir else None,
        args.max_series,
    )
    restored, problems = registry.restore_all()
    if restored:
        print(f"[anomaly-detector] restored the baseline of {restored} series from {args.baseline_dir}")
    for problem in problems:
        print(f"[anomaly-detector] WARNING: could not restore a series: {problem}", file=sys.stderr)
    server = DetectorServer((args.addr, args.port), detector, baseline_path, token=token, protect_reads=protect_reads, registry=registry)
    follower_stop = None
    if args.follow_config:
        try:
            follow_config, follow_raw = load_follow_config(args.follow_config)
            client = _follow_client(follow_config, follow_raw)
        except FollowConfigError as e:
            print(f"error: --follow-config: {e}", file=sys.stderr)
            server.server_close()
            return 2
        publisher = MqttPublisher(follow_config.mqtt) if follow_config.mqtt is not None else None
        follower = Follower(follow_config, registry, client, sample_rate=args.sample_rate, publisher=publisher)
        server.follower_snapshot = follower.snapshot
        _, follower_stop = follower.start()
        print(f"[anomaly-detector] following {len(follow_config.series)} series from {follow_config.datalake_url} every {follow_config.interval_s:g} s"
              f" (verdicts {'written back as ' + follow_config.score_kind if follow_config.write_scores else 'not written back'}, alarms {'over MQTT' if publisher else 'not published'})")
    print(f"[anomaly-detector] HTTP API listening on {args.addr}:{args.port} "
          f"(sample_rate={args.sample_rate}Hz, threshold={args.threshold}sigma, window={args.window}, "
          f"{'reads and writes' if protect_reads else 'writes'} {'need a token' if token else 'open (loopback only)'})")
    print("[anomaly-detector] POST /baseline/fit, POST /detect, GET /stats; one detector per series: POST /detectors/{sourceId}/{kind}/{field}/fit|detect|drift/init|drift/observe, GET /detectors")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if follower_stop is not None:
            follower_stop.set()
        server.server_close()
        print("[anomaly-detector] shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
