"""
Okapi BM25 over tokenized documents (in-process, no external index server).
"""

from __future__ import annotations

import math
import re
from typing import Sequence

_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def bm25_scores(
    query: str,
    documents: Sequence[str],
    *,
    k1: float = 1.5,
    b: float = 0.75,
) -> list[float]:
    """
    Return one BM25 score per document (same order as documents).
    Empty documents get score 0.
    """
    if not documents:
        return []
    q_terms = tokenize(query)
    if not q_terms:
        return [0.0] * len(documents)

    doc_tokens: list[list[str]] = [tokenize(d) for d in documents]
    doc_lens = [max(1, len(t)) for t in doc_tokens]
    avgdl = sum(doc_lens) / len(doc_lens)
    N = len(documents)

    df: dict[str, int] = {}
    for terms in doc_tokens:
        seen = set(terms)
        for t in seen:
            df[t] = df.get(t, 0) + 1

    idf: dict[str, float] = {}
    for t in set(q_terms):
        n = df.get(t, 0)
        # BM25 idf variant (smooth)
        idf[t] = math.log(1.0 + (N - n + 0.5) / (n + 0.5))

    scores: list[float] = []
    for i, terms in enumerate(doc_tokens):
        if not terms:
            scores.append(0.0)
            continue
        dl = doc_lens[i]
        tf: dict[str, int] = {}
        for t in terms:
            tf[t] = tf.get(t, 0) + 1
        s = 0.0
        for qt in q_terms:
            if qt not in tf:
                continue
            f = tf[qt]
            num = f * (k1 + 1)
            den = f + k1 * (1 - b + b * dl / avgdl)
            s += idf.get(qt, 0.0) * (num / den) if den else 0.0
        scores.append(s)
    return scores


def min_max_norm(values: list[float]) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi <= lo:
        return [0.5 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]
