"""Optional OpenAI text embeddings for hybrid retrieval reranking."""

from __future__ import annotations

import json
import logging
import ssl
import urllib.request
from typing import Sequence

log = logging.getLogger(__name__)


def fetch_embeddings(
    texts: Sequence[str],
    *,
    api_key: str,
    model: str = "text-embedding-3-small",
    timeout_s: float = 45.0,
) -> list[list[float]] | None:
    """Batch embedding; returns None on failure."""
    if not api_key or not texts:
        return None
    clean = [t[:8000] if t else "" for t in texts]
    body = json.dumps({"model": model, "input": clean}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/embeddings",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key.strip()}",
        },
        method="POST",
    )
    try:
        try:
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout_s, context=ctx) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log.debug("Embeddings request failed: %s", exc)
        return None
    data = payload.get("data") or []
    if len(data) != len(clean):
        return None
    # Preserve order by index
    indexed: list[tuple[int, list[float]]] = []
    for item in data:
        idx = int(item.get("index", 0))
        emb = item.get("embedding")
        if isinstance(emb, list):
            indexed.append((idx, [float(x) for x in emb]))
    indexed.sort(key=lambda x: x[0])
    return [e for _, e in indexed]


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
