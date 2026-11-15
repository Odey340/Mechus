# Koov API server – production-ready Linux image (Zoom SDK, gunicorn, non-root).
# Use on Mac via Docker for join_backend=zoom_sdk, or deploy to cloud.

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV KOOV_API_HOST=0.0.0.0

# System deps: Python (incl. dev headers for webrtcvad), PortAudio (sounddevice), GLib
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    python3-gi \
    libglib2.0-0 \
    libcairo2 \
    libgirepository1.0-dev \
    libportaudio2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps (Koov + production server). Zoom SDK omitted: needs Zoom's native Linux SDK (zoom_sdk.h) - see docs/ZOOM_SDK_SETUP.md.
COPY requirements.txt requirements-prod.txt requirements-chert.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt -r requirements-prod.txt -r requirements-chert.txt

# Copy app
COPY koov/ ./koov/
COPY scripts/ ./scripts/
COPY main.py ./

# One-time: download wake word (Vosk) model so join-meeting works without host setup
RUN python3 scripts/download_vosk_model.py --out models \
    && test -d models/vosk-model-small-en-us-0.15 || (echo "Vosk model missing after download" && exit 1)

# Absolute path so gunicorn finds the model regardless of CWD
ENV KOOV_VOSK_MODEL_PATH=/app/models/vosk-model-small-en-us-0.15

# Non-root user for production
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 5000

# Production: gunicorn with multiple workers (sync workers; Zoom/browser sessions are per-process)
# Workers: 1 recommended when using browser/zoom_sdk join (state in memory). Scale horizontally instead.
ENV GUNICORN_WORKERS=1
ENV GUNICORN_THREADS=4
ENV GUNICORN_BIND=0.0.0.0:5000
ENV GUNICORN_TIMEOUT=120
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "4", "--timeout", "120", "--access-logfile", "-", "--error-logfile", "-", "koov.api_server:app"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/health')" || exit 1
