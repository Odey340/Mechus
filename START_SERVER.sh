#!/bin/bash
# Start the Koov API server (development only).
# Production: run Koov in Docker and scale with multiple instances. See docs/RUN_KOOV_DOCKER.md and docs/PRODUCTION.md.
set -e
cd "$(dirname "$0")"

# Activate virtual environment
if [ -d .venv ]; then
  source .venv/bin/activate
fi

# Set Python path
export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$PWD"

# Optional: install deps if venv is missing them (run 'pip install -r requirements.txt' once)
# pip install -q flask flask-cors 2>/dev/null || true

# The Python launcher safely parses .env before importing the server.
python3 scripts/start_api_server.py
