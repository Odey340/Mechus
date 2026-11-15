#!/usr/bin/env python3
"""
Start the Koov API server.

This script starts the HTTP API server that allows the dashboard to trigger
Koov to join meetings automatically.

Usage:
    python3 scripts/start_api_server.py

Environment variables:
    KOOV_ENV_FILE: Path to .env file (default: .env)
    KOOV_API_PORT: Port to run server on (default: 5000)
    KOOV_API_HOST: Host to bind to (default: 127.0.0.1)
    KOOV_API_DEBUG: Enable debug mode (default: false)
"""

from __future__ import annotations

import os
import sys

# Add repo root to path
repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

# Load .env before importing app (so KOOV_API_PORT, KOOV_CORS_ORIGINS, etc. are set)
from koov.envfile import load_env_file
load_env_file(os.getenv("KOOV_ENV_FILE", os.path.join(repo_root, ".env")))

from koov.api_server import main

if __name__ == "__main__":
    main()
