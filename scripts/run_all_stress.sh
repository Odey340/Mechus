#!/usr/bin/env bash
# Run unit tests + retrieval/intent latency stress (CPU-only; no API keys).
# Optional: `pip install soundfile` then
#   python3 scripts/pressure_test_stt.py --backend elevenlabs --mock --iterations 20
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
python3 -m pytest tests/ -q
python3 scripts/stress_test_latency.py --quick
echo "Core stress checks passed."
