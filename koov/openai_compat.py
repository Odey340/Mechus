"""
OpenAI-compatible LLM provider selection.

Koov talks to the Chat Completions API. By default that is OpenAI, but any
OpenAI-compatible endpoint works. Built-in preset:

  KOOV_LLM_PROVIDER=vultr        -> Vultr Serverless Inference (https://api.vultrinference.com/v1)
    VULTR_INFERENCE_API_KEY=...   (used as KOOV_OPENAI_API_KEY if that is not set)
    VULTR_INFERENCE_MODEL=...     (used as KOOV_OPENAI_CHAT_MODEL if that is not set;
                                   list models with: python scripts/vultr_inference_check.py)

Or point at any compatible server yourself:
  KOOV_OPENAI_BASE_URL=https://my-gpu-box.example.com/v1   (e.g. vLLM/Ollama on a Vultr Cloud GPU)
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("koov.llm_provider")

OPENAI_BASE_URL = "https://api.openai.com/v1"
VULTR_INFERENCE_BASE_URL = "https://api.vultrinference.com/v1"


def provider() -> str:
    return (os.environ.get("KOOV_LLM_PROVIDER") or "openai").strip().lower()


def base_url() -> str:
    explicit = (os.environ.get("KOOV_OPENAI_BASE_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    if provider() == "vultr":
        return VULTR_INFERENCE_BASE_URL
    return OPENAI_BASE_URL


def chat_completions_url() -> str:
    return f"{base_url()}/chat/completions"


def origin() -> str:
    """Scheme+host for HTTP connection prewarming."""
    url = base_url()
    parts = url.split("/")
    return "/".join(parts[:3]) + "/"


def supports_json_mode() -> bool:
    """OpenAI supports response_format=json_object; other providers vary by model."""
    flag = (os.environ.get("KOOV_LLM_JSON_MODE") or "").strip().lower()
    if flag in ("1", "true", "yes"):
        return True
    if flag in ("0", "false", "no"):
        return False
    return base_url() == OPENAI_BASE_URL


def apply_provider_env() -> None:
    """Map provider-specific env vars onto the KOOV_OPENAI_* names the rest of Koov reads."""
    if provider() != "vultr":
        return
    key = (os.environ.get("VULTR_INFERENCE_API_KEY") or "").strip()
    if key and not (os.environ.get("KOOV_OPENAI_API_KEY") or "").strip():
        os.environ["KOOV_OPENAI_API_KEY"] = key
    model = (os.environ.get("VULTR_INFERENCE_MODEL") or "").strip()
    if model:
        for name in ("KOOV_OPENAI_CHAT_MODEL", "KOOV_OPENAI_RESPONSE_MODEL", "KOOV_OPENAI_THINKING_MODEL"):
            if not (os.environ.get(name) or "").strip():
                os.environ[name] = model
    elif not (os.environ.get("KOOV_OPENAI_CHAT_MODEL") or "").strip():
        log.warning(
            "KOOV_LLM_PROVIDER=vultr but no VULTR_INFERENCE_MODEL / KOOV_OPENAI_CHAT_MODEL set; "
            "run `python scripts/vultr_inference_check.py` to list models."
        )
    if not (os.environ.get("KOOV_LLM_BACKEND") or "").strip():
        os.environ["KOOV_LLM_BACKEND"] = "openai"
