#!/usr/bin/env python3
"""
Check Vultr Serverless Inference for Koov: list models and run one test completion.

  1. Vultr console -> Serverless Inference -> create a subscription, copy its API key.
  2. In .env:  KOOV_LLM_PROVIDER=vultr
               VULTR_INFERENCE_API_KEY=...
               VULTR_INFERENCE_MODEL=<a model id printed by this script>
  3. python scripts/vultr_inference_check.py

Standard library only.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from koov.envfile import load_env_file  # noqa: E402
from koov.openai_compat import base_url, chat_completions_url  # noqa: E402


def _req(method: str, url: str, key: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read() or b"{}")


def main() -> None:
    load_env_file(str(REPO_ROOT / ".env"))
    key = (os.environ.get("VULTR_INFERENCE_API_KEY") or os.environ.get("KOOV_OPENAI_API_KEY") or "").strip()
    if not key:
        sys.exit("Set VULTR_INFERENCE_API_KEY in .env")
    os.environ.setdefault("KOOV_LLM_PROVIDER", "vultr")
    print(f"Endpoint: {base_url()}")

    models: list[str] = []
    for path in ("/models", "/chat/models"):
        try:
            data = _req("GET", base_url() + path, key)
            items = data.get("data") or data.get("models") or []
            models = [m.get("id") or m.get("name") for m in items if isinstance(m, dict)]
            if models:
                break
        except urllib.error.HTTPError:
            continue
    print("Available models:")
    for m in models:
        print(f"  {m}")

    model = (os.environ.get("VULTR_INFERENCE_MODEL") or os.environ.get("KOOV_OPENAI_CHAT_MODEL") or "").strip()
    if not model:
        sys.exit("\nPick one and set VULTR_INFERENCE_MODEL=<id> in .env, then rerun for a test call.")
    t0 = time.monotonic()
    try:
        out = _req("POST", chat_completions_url(), key, {
            "model": model,
            "messages": [
                {"role": "system", "content": "You are Koov, a concise meeting assistant."},
                {"role": "user", "content": "In one sentence: what are the action items if Sam owns the demo and Priya owns the slides?"},
            ],
            "max_tokens": 80,
        })
    except urllib.error.HTTPError as exc:
        sys.exit(f"Completion failed: HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}")
    ms = (time.monotonic() - t0) * 1000
    print(f"\n{model} answered in {ms:.0f} ms:\n  {out['choices'][0]['message']['content'].strip()}")


if __name__ == "__main__":
    main()
