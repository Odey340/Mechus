#!/usr/bin/env python3
"""
Stress-test and benchmark latency on the production hot path:

  classify_intent → ContextIntegrityEngine.ingest_chunk → retrieve_for_question

No network calls unless --hybrid-retrieval and MECHUS_OPENAI_API_KEY is set (embedding rerank).

Examples:
  # Default: large meeting + many retrieval iterations (lexical only, CPU-bound)
  python3 scripts/stress_test_latency.py

  # Quick smoke (~2s)
  python3 scripts/stress_test_latency.py --quick

  # Stress concurrent meetings (separate engines; no shared lock)
  python3 scripts/stress_test_latency.py --workers 8 --iterations 30

  # Compare hybrid (embeddings API) vs lexical — expect higher p95 for hybrid
  python3 scripts/stress_test_latency.py --hybrid-retrieval --iterations 20

Lowest latency in production: set MECHUS_LOW_LATENCY=1 (see mechus/config.py) or manually:
  MECHUS_RETRIEVAL_MODE=lexical
  MECHUS_RETRIEVAL_EMBED_WEIGHT=0
  MECHUS_RETRIEVAL_MAX_CHARS=3000
  MECHUS_RESPONSE_OPENAI_TIMEOUT_S=12
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


def _percentiles_ms(samples: list[float]) -> dict[str, float]:
    if not samples:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
    s = sorted(samples)
    n = len(s)

    def pct(p: float) -> float:
        idx = min(int(n * p), n - 1)
        return s[min(idx, n - 1)] * 1000.0

    return {
        "p50": s[n // 2] * 1000.0,
        "p95": pct(0.95),
        "p99": pct(0.99),
        "max": s[-1] * 1000.0,
    }


def _bench_intent(iterations: int) -> dict[str, float]:
    from mechus.intent import classify_intent

    phrases = [
        "recap the last five minutes",
        "what did we decide about pricing",
        "who owns the API migration",
        "what are the risks",
        "hello are you there",
    ]
    lat: list[float] = []
    t0 = time.perf_counter()
    for i in range(iterations):
        s = time.perf_counter()
        classify_intent(phrases[i % len(phrases)])
        lat.append(time.perf_counter() - s)
    wall = time.perf_counter() - t0
    p = _percentiles_ms(lat)
    p["ops_per_sec"] = iterations / wall if wall > 0 else 0.0
    return p


def _build_engine(
    n_chunks: int,
    *,
    seed: float = 1_700_000_000.0,
) -> tuple[object, list[str]]:
    from mechus.context_integrity_engine import ContextIntegrityEngine

    eng = ContextIntegrityEngine(meeting_id="stress-latency")
    lines: list[str] = []
    for i in range(n_chunks):
        ts = seed + float(i) * 2.0
        text = (
            f"Speaker segment {i}: we discussed budget item {i % 7} and "
            f"the timeline for milestone {i % 5}."
        )
        eng.ingest_chunk(text, speaker_id=f"S{i % 4}", timestamp=ts)
        lines.append(text)
    return eng, lines


def _bench_retrieval_one(
    eng,
    iterations: int,
    queries: list[str],
    *,
    mode: str,
    api_key: str | None,
) -> dict[str, float]:
    from mechus.retrieval import retrieve_for_question

    lat: list[float] = []
    for i in range(iterations):
        q = queries[i % len(queries)]
        t0 = time.perf_counter()
        retrieve_for_question(
            q,
            context_engine=eng,
            memory=None,
            max_chars=4000,
            general_max_chars=3200,
            retrieval_mode=mode,
            openai_api_key=api_key,
        )
        lat.append(time.perf_counter() - t0)
    return _percentiles_ms(lat)


def _worker_retrieval(
    worker_id: int,
    n_chunks: int,
    iterations: int,
    queries: list[str],
    mode: str,
    api_key: str | None,
) -> dict[str, float]:
    eng, _ = _build_engine(n_chunks, seed=1_700_000_000.0 + worker_id * 10_000.0)
    return _bench_retrieval_one(eng, iterations, queries, mode=mode, api_key=api_key)


def main() -> int:
    ap = argparse.ArgumentParser(description="Stress-test Mechus retrieval + intent latency.")
    ap.add_argument("--chunks", type=int, default=400, help="Transcript chunks per simulated meeting")
    ap.add_argument("--iterations", type=int, default=80, help="Retrieval calls per engine / worker")
    ap.add_argument("--intent-iterations", type=int, default=5000, help="classify_intent calls")
    ap.add_argument("--workers", type=int, default=1, help="Parallel engines (each with --chunks)")
    ap.add_argument("--quick", action="store_true", help="Small chunks/iterations for CI smoke")
    ap.add_argument(
        "--hybrid-retrieval",
        action="store_true",
        help="Use hybrid retrieval (requires MECHUS_OPENAI_API_KEY for embeddings)",
    )
    args = ap.parse_args()

    if args.quick:
        args.chunks = 80
        args.iterations = 25
        args.intent_iterations = 800
        args.workers = min(args.workers, 2)

    mode = "hybrid" if args.hybrid_retrieval else "lexical"
    api_key = (os.getenv("MECHUS_OPENAI_API_KEY") or "").strip() or None
    if mode == "hybrid" and not api_key:
        print("Warning: --hybrid-retrieval but MECHUS_OPENAI_API_KEY unset; falling back to lexical.")
        mode = "lexical"

    queries = [
        "Mechus give me a recap of the meeting",
        "what did we say about budget",
        "recap the last 10 minutes",
        "list action items",
        "what risks were mentioned",
    ]

    print("Mechus latency stress")
    print(f"  chunks={args.chunks}  retrieval_iters={args.iterations}  intent_iters={args.intent_iterations}")
    print(f"  workers={args.workers}  retrieval_mode={mode}")
    print(f"  MECHUS_LOW_LATENCY={os.getenv('MECHUS_LOW_LATENCY', '')!r}")
    print()

    # 1) Intent
    ip = _bench_intent(args.intent_iterations)
    print("classify_intent:")
    print(f"  p50={ip['p50']:.4f} ms  p95={ip['p95']:.4f} ms  p99={ip['p99']:.4f} ms  (per call)")
    print(f"  throughput: {ip['ops_per_sec']:.0f} ops/sec")
    print()

    # 2) Build one large engine + retrieval
    t_build0 = time.perf_counter()
    eng, _ = _build_engine(args.chunks)
    build_ms = (time.perf_counter() - t_build0) * 1000.0
    print(f"ingest {args.chunks} chunks (single meeting): {build_ms:.1f} ms")
    rp = _bench_retrieval_one(eng, args.iterations, queries, mode=mode, api_key=api_key)
    print(f"retrieve_for_question ({mode}, single engine) p50={rp['p50']:.2f} ms  p95={rp['p95']:.2f} ms  p99={rp['p99']:.2f} ms  max={rp['max']:.2f} ms")
    print()

    # 3) Parallel workers
    if args.workers > 1:
        t0 = time.perf_counter()
        results: list[dict[str, float]] = []
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = [
                ex.submit(
                    _worker_retrieval,
                    w,
                    args.chunks,
                    args.iterations,
                    queries,
                    mode,
                    api_key,
                )
                for w in range(args.workers)
            ]
            for f in as_completed(futs):
                results.append(f.result())
        wall = time.perf_counter() - t0
        all_p95 = [r["p95"] for r in results]
        print(f"Parallel {args.workers} workers (same total iters per worker): wall {wall:.2f}s")
        print(f"  per-worker p95 retrieval: min={min(all_p95):.2f} ms  max={max(all_p95):.2f} ms")
        print()

    print("Tips for lowest latency:")
    print("  export MECHUS_LOW_LATENCY=1")
    print("  # or: MECHUS_RETRIEVAL_MODE=lexical, MECHUS_RETRIEVAL_EMBED_WEIGHT=0")
    print("  # STT: smaller whisper model / faster compute; shorter transcript chunks")
    print("  # LLM: lower MECHUS_RESPONSE_OPENAI_MAX_TOKENS, shorter MECHUS_RESPONSE_PROMPT_MAX_CHARS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
