# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - Container Build: Dockerfile
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
# The detector as a long-running service, like the rest of the family. The only runtime dependency is numpy.
#
# Inside a container the service must listen on every interface to be reached through a published port, and the detector refuses
# to do that without a bearer token (nobody who reaches the port may replace the baseline every reading is judged against): pass it as
# HYDRA_UMC_ANOMALY_TOKEN (16 or more characters) or mount a file and add --token-file. The baselines live in the /data volume,
# one per series. Publish the port on the loopback of the host (-p 127.0.0.1:8097:8097) unless something with its own
# authentication sits in front.
#
#   docker build -t hydra-umc-anomaly-detector .
#   docker run --rm -e HYDRA_UMC_ANOMALY_TOKEN='a-secret-of-16-or-more-characters' -p 127.0.0.1:8097:8097 \
#       -v anomaly-detector-data:/data hydra-umc-anomaly-detector
#
# Continuous service mode: mount a follow configuration and add its path, e.g. ... hydra-umc-anomaly-detector --follow-config /config/follow.json

FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md CHANGELOG.md LICENSE.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --system --uid 10001 hydra && mkdir /data && chown hydra /data
USER hydra
VOLUME ["/data"]

EXPOSE 8097
HEALTHCHECK --interval=15s --timeout=5s --retries=5 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8097/stats', timeout=3)"]
ENTRYPOINT ["hydra-umc-anomaly-detector"]
CMD ["--addr", "0.0.0.0", "--port", "8097", "--baseline-dir", "/data/series", "--baseline-path", "/data/baseline.npz"]
