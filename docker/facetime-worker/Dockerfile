# Koov FaceTime worker – Linux container that runs:
# - PulseAudio virtual devices for per-meeting routing
# - Xvfb (headed Chromium without a physical display)
# - Chromium + chromedriver (Selenium)
# - Koov Python worker (koov.facetime_worker)
#
# Build:
#   docker build -t koov-facetime-worker:latest -f docker/facetime-worker/Dockerfile .
#
# Run (example):
#   docker run --name koov-facetime-<id> -e KOOV_MEETING_ID=... -e KOOV_MEETING_LINK=... -e KOOV_DISPLAY_NAME=Koov koov-facetime-worker:latest

FROM debian:12-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
# Debian 12+ blocks system-wide pip (PEP 668); use a venv for application deps.
ENV VIRTUAL_ENV=/opt/koov-venv
ENV PATH="/opt/koov-venv/bin:$PATH"

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv python3-dev \
    # Koov audio deps
    libportaudio2 \
    # PulseAudio + tooling
    pulseaudio pulseaudio-utils \
    # X server for headed browser
    xvfb xauth \
    # Browser + driver
    chromium chromium-driver \
    # Generated virtual camera + diagnostics (host provides v4l2loopback device)
    ffmpeg v4l-utils \
    # Common runtime libs for Chromium/WebRTC
    ca-certificates fonts-liberation \
    libasound2 libatk-bridge2.0-0 libatk1.0-0 libc6 libcairo2 libcups2 libdbus-1-3 \
    libdrm2 libexpat1 libgbm1 libglib2.0-0 libnspr4 libnss3 libpango-1.0-0 \
    libpangocairo-1.0-0 libx11-6 libx11-xcb1 libxcb1 libxcomposite1 libxdamage1 \
    libxext6 libxfixes3 libxkbcommon0 libxrandr2 libxrender1 libxshmfence1 \
    && rm -rf /var/lib/apt/lists/*

RUN ffmpeg -version >/dev/null && v4l2-ctl --version >/dev/null

WORKDIR /app

COPY requirements.txt requirements-prod.txt ./
# webrtcvad has no wheel for manylinux; compiling from source needs gcc. Purge toolchain after pip.
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && python3 -m venv /opt/koov-venv \
    && /opt/koov-venv/bin/pip install --no-cache-dir --upgrade pip setuptools wheel \
    && /opt/koov-venv/bin/pip install --no-cache-dir -r requirements.txt -r requirements-prod.txt \
    && apt-get purge -y build-essential \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Optional Vosk model: fetch before COPY koov/ so app code changes do not re-trigger a ~40MB download.
COPY scripts/download_vosk_model.py ./scripts/download_vosk_model.py
RUN mkdir -p models \
    && python3 scripts/download_vosk_model.py --out models \
    && test -d models/vosk-model-small-en-us-0.15 || true \
    && rm -f models/vosk-model-small-en-us-0.15.zip 2>/dev/null || true

COPY koov/ ./koov/
COPY scripts/ ./scripts/
COPY main.py ./

ENV KOOV_VOSK_MODEL_PATH=/app/models/vosk-model-small-en-us-0.15

COPY docker/facetime-worker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV DISPLAY=:99

ENTRYPOINT ["/entrypoint.sh"]
