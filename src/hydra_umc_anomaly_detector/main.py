# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - src/hydra_umc_anomaly_detector/main.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
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
import sys

from . import __version__
from .api import DetectorServer
from .detector import AnomalyDetector

PROJECT_NAME = "HYDRA-UMC-ANOMALY-DETECTOR"
ROLE = (
    "Anomaly-Detector - AI-driven predictive maintenance, FFT/spectrogram "
    "analysis of motor vibration signatures fed by HYDRA-UMC-DATALAKE."
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hydra-umc-anomaly-detector")
    # Real gap found by an ecosystem-wide audit: this used to default to
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
    args = parser.parse_args(argv)

    print(f"{PROJECT_NAME} v{__version__}")
    print(ROLE)

    detector = AnomalyDetector(sample_rate=args.sample_rate, threshold=args.threshold)
    server = DetectorServer((args.addr, args.port), detector)
    print(f"[anomaly-detector] HTTP API listening on {args.addr}:{args.port} "
          f"(sample_rate={args.sample_rate}Hz, threshold={args.threshold}sigma)")
    print("[anomaly-detector] POST /baseline/fit, POST /detect, GET /stats")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("[anomaly-detector] shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
