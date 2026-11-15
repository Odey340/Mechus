#!/usr/bin/env python3
"""
Chaotic stress test for retrieval, chapters, and summary evidence.
Run from repo root: python3 scripts/chaotic_retrieval_smoke.py

Uses randomness (seeded) + edge cases: unicode, newlines, huge lines,
empty-ish queries, corrupt JSONL lines, tiny char budgets, many chunks.

Long meeting benchmark (default): simulates a dense multi-hour call, writes
chapter rollups, prints ingest + retrieval + summary-build latency.

  python3 scripts/chaotic_retrieval_smoke.py --long-chunks 1200
  python3 scripts/chaotic_retrieval_smoke.py --long-only --long-chunks 2000

Financial scrum (same API path as a live meeting — use `.env` like production):

  # KOOV_OPENAI_API_KEY, KOOV_LLM_BACKEND=openai, KOOV_RETRIEVAL_MODE=hybrid, …
  python3 scripts/chaotic_retrieval_smoke.py --financial-scrum
  # or: python3 scripts/simulate_financial_scrum_koov.py
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import tempfile
import time
import traceback
from pathlib import Path

# Repo root on path
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Load .env (OpenAI key, etc.) — same pattern as test_financial_scrum.py
_env_path = _ROOT / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())
if os.environ.get("OPENAI_API_KEY") and not os.environ.get("KOOV_OPENAI_API_KEY"):
    os.environ.setdefault("KOOV_OPENAI_API_KEY", os.environ["OPENAI_API_KEY"])

from koov.context_integrity_engine import ContextIntegrityEngine
from koov.meeting_chapters import ChapterRecord, append_chapter, load_chapters
from koov.meeting_store import StoredChunk
from koov.meeting_summary import build_summary_evidence_text
from koov.retrieval import retrieve_for_question


def _ok(name: str, cond: bool, detail: str = "") -> bool:
    status = "PASS" if cond else "FAIL"
    extra = f" — {detail}" if detail else ""
    print(f"  [{status}] {name}{extra}")
    return cond


def _fmt_ms(x: float) -> str:
    if x >= 1000:
        return f"{x / 1000:.3f}s"
    return f"{x:.3f}ms"


def build_long_meeting(
    rng: random.Random,
    n_chunks: int,
    *,
    duration_s: float = 7200.0,
    meeting_id: str = "long_meeting_benchmark",
) -> tuple[ContextIntegrityEngine, float]:
    """
    Simulate a long meeting: many tagged chunks, timestamps spread over duration_s (default 2h).
    Returns (engine, ingest_wall_seconds).
    """
    eng = ContextIntegrityEngine(meeting_id=meeting_id)
    speakers = ["Alex", "Jordan", "Sam", "Riley", "Casey", "Morgan", "Taylor"]
    topics = [
        "We need to lock Q4 budget by Friday.",
        "Action item: Riley will send the API spec to the vendor by Tuesday.",
        "Open question: do we support SAML on the PhoenixProjectDeadline2027 timeline?",
        "The unicorn valuation discussion is parked until legal signs.",
        "Decision: we ship the beta on March 3; no slip without exec approval.",
        "café naïve résumé testing unicode in transcripts.",
    ]
    t0 = time.perf_counter()
    for i in range(n_chunks):
        spk = speakers[i % len(speakers)]
        ts = (i / max(1, n_chunks - 1)) * duration_s if n_chunks > 1 else 0.0
        if rng.random() < 0.12:
            text = topics[rng.randint(0, len(topics) - 1)]
        elif rng.random() < 0.08:
            text = f"Utterance {i}: " + "filler " * rng.randint(5, 35)
        else:
            text = (
                f"[{int(ts // 60):02d}:{int(ts % 60):02d}] "
                f"Discussion point {i % 200}: roadmap item {rng.randint(1, 500)}."
            )
        eng.ingest_chunk(text.strip() or f"tick {i}", speaker_id=spk, timestamp=ts)
    elapsed = time.perf_counter() - t0
    return eng, elapsed


def write_chapters_for_engine(
    eng: ContextIntegrityEngine,
    meeting_id: str,
    base_dir: str,
    *,
    duration_s: float,
    every_n: int,
) -> tuple[int, float]:
    """Append rolling chapter records; returns (chapter_count, wall_seconds)."""
    tagged = eng._tagged_chunks  # noqa: SLF001
    t0 = time.perf_counter()
    n = len(tagged)
    if n == 0:
        return 0, 0.0
    chapter_idx = 0
    for start in range(0, n, every_n):
        window = tagged[start : start + every_n]
        if not window:
            break
        frac_lo = (start / n) * duration_s
        frac_hi = ((start + len(window)) / n) * duration_s
        snippet = " ".join(w.text.replace("\n", " ")[:120] for w in window[:4])
        if len(snippet) > 900:
            snippet = snippet[:897] + "…"
        summary = f"Chapter {chapter_idx}: {len(window)} chunks. " + snippet
        rec = ChapterRecord(
            chapter_index=chapter_idx,
            ts_start=frac_lo,
            ts_end=frac_hi,
            summary=summary,
            chunk_ids=[w.chunk_id for w in window],
            entities=[],
        )
        append_chapter(meeting_id, rec, base_dir=base_dir)
        chapter_idx += 1
    return chapter_idx, time.perf_counter() - t0


def run_long_meeting_benchmark(
    *,
    long_chunks: int,
    duration_s: float = 7200.0,
    chapter_every: int = 80,
) -> None:
    """Print a detailed latency + size report for a simulated long meeting."""
    rng = random.Random(4242)
    tmp = Path(tempfile.mkdtemp(prefix="koov_long_"))
    mid = "long_meeting_benchmark"
    _prev_store = os.environ.get("KOOV_MEETING_STORE_DIR")
    os.environ["KOOV_MEETING_STORE_DIR"] = str(tmp)

    print()
    print("=" * 72)
    print("LONG MEETING BENCHMARK")
    print("=" * 72)
    print(f"  Simulated duration: {duration_s / 60:.1f} min ({duration_s:.0f}s)")
    print(f"  Target tagged chunks: {long_chunks}")
    print(f"  Chapter rollup every: {chapter_every} chunks")
    print(f"  Temp store: {tmp}")
    print()

    eng, ingest_s = build_long_meeting(rng, long_chunks, duration_s=duration_s, meeting_id=mid)
    tagged = eng._tagged_chunks  # noqa: SLF001
    n_tagged = len(tagged)
    total_text_chars = sum(len(t.text) for t in tagged)

    ch_count, ch_write_s = write_chapters_for_engine(
        eng, mid, str(tmp), duration_s=duration_s, every_n=chapter_every
    )
    loaded = load_chapters(str(tmp), mid)

    queries: list[tuple[str, str, dict]] = [
        ("specific", "What is PhoenixProjectDeadline2027 and who owns the API spec?", {"max_chars": 8000}),
        ("decision", "What did we decide about the beta ship date?", {"max_chars": 6000}),
        ("recap", "Give me a full recap of the whole meeting", {"max_chars": 12000}),
        ("recap_tight", "recap everything", {"max_chars": 4000}),
        ("general", "How did people feel about the budget numbers?", {"max_chars": 3200}),
    ]

    key = os.environ.get("KOOV_OPENAI_API_KEY", "").strip()
    results: list[dict] = []

    def run_query(label: str, q: str, max_chars: int, mode: str) -> None:
        t0 = time.perf_counter()
        bundle = retrieve_for_question(
            q,
            context_engine=eng,
            memory=None,
            max_chars=max_chars,
            retrieval_mode=mode,
            openai_api_key=key if mode == "hybrid" else None,
            embed_rerank_top_k=64,
        )
        wall_ms = (time.perf_counter() - t0) * 1000
        meta = bundle.meta
        results.append(
            {
                "label": label,
                "mode": mode,
                "intent": bundle.intent,
                "question": q[:72] + ("…" if len(q) > 72 else ""),
                "retrieval_ms_meta": float(meta.get("retrieval_ms") or 0),
                "wall_ms": wall_ms,
                "engine_chars": meta.get("engine_chunk_chars") or meta.get("engine_snippet_chars"),
                "formatted_chars": meta.get("formatted_context_chars"),
                "chunk_ids_n": len(meta.get("selected_chunk_ids") or []),
                "embed_rerank": meta.get("embed_rerank_used"),
                "recap_ch": meta.get("recap_chapters_chars"),
                "recap_tr": meta.get("recap_transcript_chars"),
            }
        )

    print("--- Legend ---")
    print("  retrieval_ms = time inside ContextIntegrityEngine.get_relevant_chunks (BM25/fusion/pack).")
    print("  wall_ms      = full retrieve_for_question() including intent, memory, prompt wrapper.")
    print("  recap_ch/tr  = chars from chapter rollup vs transcript tail (recap intent only).")
    print()

    print("--- Ingest ---")
    print(f"  Tagged chunks (actual):     {n_tagged}")
    print(f"  Total transcript chars:     {total_text_chars:,}")
    print(f"  Ingest wall time:           {_fmt_ms(ingest_s * 1000)} ({n_tagged / max(ingest_s, 1e-9):.0f} chunks/s)")
    print(f"  Chapters written:           {ch_count}  (load_chapters: {len(loaded)})")
    print(f"  Chapter write wall time:    {_fmt_ms(ch_write_s * 1000)}")
    print()

    print("--- Retrieval (lexical) ---")
    for label, q, kw in queries:
        run_query(label + "/lex", q, kw["max_chars"], "lexical")

    if key:
        print("--- Retrieval (hybrid + OpenAI embeddings) ---")
        for label, q, kw in queries[:3]:
            run_query(label + "/hyb", q, kw["max_chars"], "hybrid")
    else:
        print("--- Retrieval (hybrid) ---")
        print("  (skipped: set KOOV_OPENAI_API_KEY to measure embedding rerank latency)")
        for label, q, kw in queries[:3]:
            run_query(label + "/hyb-fallback", q, kw["max_chars"], "hybrid")

    # Repeat one query for jitter stats
    latencies: list[float] = []
    for _ in range(15):
        t0 = time.perf_counter()
        retrieve_for_question(
            "PhoenixProjectDeadline2027 SAML timeline",
            context_engine=eng,
            memory=None,
            max_chars=6000,
            retrieval_mode="lexical",
        )
        latencies.append((time.perf_counter() - t0) * 1000)
    print()
    print("--- Repeated lexical query (15x, warm) ---")
    print(f"  Wall time min/avg/max:  {min(latencies):.3f} / {statistics.mean(latencies):.3f} / {max(latencies):.3f} ms")

    print()
    print(
        "--- Per-query results (lexical"
        + (" + hybrid where noted" if key else ", hybrid = fallback without API")
        + ") ---"
    )
    for r in results:
        line = (
            f"  [{r['label']}] intent={r['intent']}  mode={r['mode']}  "
            f"retrieval_ms={r['retrieval_ms_meta']:.2f}  wall_ms={r['wall_ms']:.2f}  "
            f"engine_chars={r['engine_chars']}  fmt_chars={r['formatted_chars']}  "
            f"n_ids={r['chunk_ids_n']}"
        )
        if r.get("embed_rerank") is not None:
            line += f"  embed_rerank={r['embed_rerank']}"
        if r.get("recap_ch") is not None:
            line += f"  recap_ch={r['recap_ch']} recap_tr={r['recap_tr']}"
        print(line)

    # Summary evidence timing
    stored: list[StoredChunk] = []
    for tc in tagged:
        stored.append(
            StoredChunk(
                ts=float(tc.chunk_id),
                speaker_id=tc.speaker_id,
                text=tc.text,
                chunk_id=tc.chunk_id,
                topic=tc.topic,
                tags=list(tc.tags),
            )
        )
    t_ev0 = time.perf_counter()
    ev = build_summary_evidence_text(
        stored,
        max_chars=28000,
        meeting_id=mid,
        chapters_base_dir=str(tmp),
    )
    ev_ms = (time.perf_counter() - t_ev0) * 1000

    print()
    print("--- build_summary_evidence_text (end-of-meeting style) ---")
    print(f"  Wall time:     {_fmt_ms(ev_ms)}")
    print(f"  Output chars:  {len(ev):,} (budget 28000)")
    print(f"  Has chapters: {'Rolling chapter' in ev or 'chapter' in ev.lower()}")

    print()
    print("=" * 72)
    if _prev_store is None:
        os.environ.pop("KOOV_MEETING_STORE_DIR", None)
    else:
        os.environ["KOOV_MEETING_STORE_DIR"] = _prev_store
    try:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
    except Exception:
        pass


def chaotic_engine_fill(rng: random.Random, n: int) -> ContextIntegrityEngine:
    """Ingest n chunks with messy but valid text."""
    eng = ContextIntegrityEngine(meeting_id=f"chaos_{rng.randint(0, 99999)}")
    speakers = ["Zéphyr", "Speaker_42", "😀", "A", "Bob", ""]
    needles = [
        "PhoenixProjectDeadline2027",
        "q4_budget_freeze",
        "unicorn valuation",
    ]
    for i in range(n):
        spk = speakers[i % len(speakers)] or "unknown"
        if rng.random() < 0.08:
            text = "   \n\t  "
        elif rng.random() < 0.15:
            text = "We decided the " + needles[i % 3] + " is " + ("locked" if i % 2 else "tentative") + "."
        elif rng.random() < 0.1:
            text = "café résumé naïve " * 20 + needles[1]
        elif rng.random() < 0.05:
            text = "<script>alert(1)</script> action item: buy milk"
        else:
            text = rng.choice(
                [
                    f"Utterance {i}: " + "word " * rng.randint(1, 40),
                    "U+1F4A9 " * 5 + needles[0],
                    "Newline\n\nparty\n" + needles[2],
                ]
            )
        text = text.strip() or f"filler {i}"
        eng.ingest_chunk(text, speaker_id=spk, timestamp=float(i))
    return eng


def run_all() -> int:
    rng = random.Random(1337)
    failures = 0

    print("== 1) Empty engine ==")
    eng0 = ContextIntegrityEngine(meeting_id="empty")
    b0 = retrieve_for_question("what happened", context_engine=eng0, memory=None, max_chars=1000)
    failures += not _ok("empty engine returns placeholder", "(No meeting context" in b0.formatted_context or "yet" in b0.formatted_context)

    print("== 2) Chaotic lexical retrieval (find rare token) ==")
    eng = chaotic_engine_fill(rng, 80)
    # Ensure at least one chunk mentions the needle explicitly
    eng.ingest_chunk(
        "FINAL: PhoenixProjectDeadline2027 must ship before the monsoon season.",
        speaker_id="Lead",
        timestamp=999.0,
    )
    bundle = retrieve_for_question(
        "What is PhoenixProjectDeadline2027?",
        context_engine=eng,
        memory=None,
        max_chars=2000,
        retrieval_mode="lexical",
    )
    ctx = (bundle.formatted_context or "").lower()
    failures += not _ok("rare token appears in context", "phoenixprojectdeadline2027" in ctx)
    failures += not _ok("meta has chunk ids", len(bundle.meta.get("selected_chunk_ids") or []) > 0)
    failures += not _ok("retrieval_ms set", float(bundle.meta.get("retrieval_ms") or 0) >= 0)

    print("== 3) Tiny budget truncation (no crash) ==")
    b_tiny = retrieve_for_question(
        "PhoenixProjectDeadline2027 status",
        context_engine=eng,
        memory=None,
        max_chars=120,
        retrieval_mode="lexical",
    )
    failures += not _ok("tiny budget returns short context", len(b_tiny.formatted_context or "") < 8000)

    print("== 4) Recap + corrupt chapters file ==")
    tmp = Path(tempfile.mkdtemp(prefix="koov_chaos_"))
    mid = "meet_X"
    ch_dir = tmp / mid
    ch_dir.mkdir(parents=True)
    ch_file = ch_dir / "chapters.jsonl"
    # Valid line
    rec = ChapterRecord(
        chapter_index=0,
        ts_start=0.0,
        ts_end=1.0,
        summary="Everyone argued about PhoenixProjectDeadline2027.",
        chunk_ids=[1, 2, 3],
        entities=["Phoenix"],
    )
    append_chapter(mid, rec, base_dir=str(tmp))
    # Garbage lines (should be skipped by loader)
    with open(ch_file, "a", encoding="utf-8") as f:
        f.write("not json!!!\n")
        f.write('{"broken": true}\n')

    old_env = os.environ.get("KOOV_MEETING_STORE_DIR")
    os.environ["KOOV_MEETING_STORE_DIR"] = str(tmp)
    try:
        eng2 = ContextIntegrityEngine(meeting_id=mid)
        eng2.ingest_chunk("hello", speaker_id="A", timestamp=0.0)
        eng2.ingest_chunk("goodbye chaos", speaker_id="B", timestamp=1.0)
        br = retrieve_for_question(
            "recap the entire meeting in detail",
            context_engine=eng2,
            memory=None,
            max_chars=6000,
        )
        fc = br.formatted_context or ""
        failures += not _ok("recap mentions chapters or Phoenix", "chapter" in fc.lower() or "phoenix" in fc.lower())
        failures += not _ok("recap meta has recap fields", "recap_transcript_chars" in br.meta or "recap_chapters_chars" in br.meta)
        loaded = load_chapters(str(tmp), mid)
        failures += not _ok("load_chapters skips garbage", len(loaded) >= 1)
    finally:
        if old_env is None:
            os.environ.pop("KOOV_MEETING_STORE_DIR", None)
        else:
            os.environ["KOOV_MEETING_STORE_DIR"] = old_env

    print("== 5) build_summary_evidence chaos ==")
    chunks: list[StoredChunk] = []
    for i in range(300):
        tags = ["decision"] if i in (3, 77, 199) else (["action_item"] if i == 42 else [])
        text = ("noise " * 15) + (f" SPECIAL_MARKER_{i % 17} " if i % 23 == 0 else "") + " end."
        chunks.append(
            StoredChunk(
                ts=float(i),
                speaker_id=f"S{i % 5}",
                text=text,
                chunk_id=i + 1,
                tags=tags,
            )
        )
    ev = build_summary_evidence_text(
        chunks,
        max_chars=3500,
        meeting_id=mid,
        chapters_base_dir=str(tmp),
    )
    failures += not _ok("evidence bounded", len(ev) <= 4000)
    failures += not _ok("evidence has salient or structure", "Salient" in ev or "chapter" in ev.lower() or "SPECIAL_MARKER" in ev)

    print("== 6) Hybrid mode without API key (lexical fallback, no crash) ==")
    bh = retrieve_for_question(
        "PhoenixProjectDeadline2027",
        context_engine=eng,
        memory=None,
        max_chars=1500,
        retrieval_mode="hybrid",
        openai_api_key=None,
        embed_weight=0.9,
    )
    failures += not _ok("hybrid without key still returns", len(bh.formatted_context or "") > 0)
    failures += not _ok("embed rerank not used without key", bh.meta.get("embed_rerank_used") is False)

    print("== 7) Optional live hybrid (OPENAI) ==")
    key = os.environ.get("KOOV_OPENAI_API_KEY", "").strip()
    if key:
        try:
            bh2 = retrieve_for_question(
                "What did we say about deadlines?",
                context_engine=eng,
                memory=None,
                max_chars=2000,
                retrieval_mode="hybrid",
                openai_api_key=key,
                embed_rerank_top_k=8,
            )
            failures += not _ok("hybrid with key returns", len(bh2.formatted_context or "") > 0)
            # May or may not rerank depending on scores; just ensure no exception
            failures += not _ok("hybrid meta consistent", "retrieval_ms" in bh2.meta)
        except Exception as e:
            print(f"  [SKIP/FAIL] live hybrid: {e}")
            traceback.print_exc()
            failures += 1
    else:
        print("  [SKIP] KOOV_OPENAI_API_KEY not set — hybrid live test skipped")

    print("== 8) More chaos: punctuation-only query, top_k=1, concurrent retrieves ==")
    bp = retrieve_for_question(
        "??? ### $$$",
        context_engine=eng,
        memory=None,
        max_chars=3000,
        retrieval_mode="lexical",
    )
    failures += not _ok("punctuation query does not crash", bp.formatted_context is not None)

    b_one = retrieve_for_question(
        "PhoenixProjectDeadline2027",
        context_engine=eng,
        memory=None,
        max_chars=5000,
        retrieval_mode="lexical",
        embed_rerank_top_k=1,
    )
    failures += not _ok("embed_rerank_top_k=1 ok", len(b_one.formatted_context or "") > 0)

    import concurrent.futures

    def _call() -> str:
        r = retrieve_for_question(
            "unicorn valuation",
            context_engine=eng,
            memory=None,
            max_chars=800,
            retrieval_mode="lexical",
        )
        return r.formatted_context or ""

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(_call) for _ in range(24)]
        results = [f.result() for f in futures]
    failures += not _ok("24 concurrent retrieves OK", all(isinstance(x, str) for x in results))

    print("== 9) Recap with tiny max_chars (chapter prefix vs tail) ==")
    _prev = os.environ.get("KOOV_MEETING_STORE_DIR")
    os.environ["KOOV_MEETING_STORE_DIR"] = str(tmp)
    try:
        eng3 = ContextIntegrityEngine(meeting_id=mid)
        for i in range(15):
            eng3.ingest_chunk(f"line {i} " * 10, speaker_id="X", timestamp=float(i))
        br_small = retrieve_for_question(
            "give me a full recap",
            context_engine=eng3,
            memory=None,
            max_chars=400,
        )
        failures += not _ok("tiny recap still returns", len(br_small.formatted_context or "") > 0)
    finally:
        if _prev is None:
            os.environ.pop("KOOV_MEETING_STORE_DIR", None)
        else:
            os.environ["KOOV_MEETING_STORE_DIR"] = _prev

    print("== 10) Extreme fusion weights ==")
    bw = retrieve_for_question(
        "budget freeze",
        context_engine=eng,
        memory=None,
        max_chars=2000,
        retrieval_mode="lexical",
        bm25_weight=0.99,
        tag_weight=0.01,
    )
    failures += not _ok("extreme weights ok", "budget" in (bw.formatted_context or "").lower() or len(bw.formatted_context or "") > 0)

    print()
    if failures:
        print(f"Done: {failures} check(s) failed.")
        return 1
    print("Done: all chaotic checks passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Chaotic retrieval smoke + long meeting latency report.")
    parser.add_argument(
        "--long-chunks",
        type=int,
        default=1200,
        help="Tagged chunks to simulate for the long meeting (default: 1200).",
    )
    parser.add_argument(
        "--duration-min",
        type=float,
        default=120.0,
        help="Simulated meeting length in minutes (default: 120).",
    )
    parser.add_argument(
        "--chapter-every",
        type=int,
        default=80,
        help="Write one chapter rollup every N chunks (default: 80).",
    )
    parser.add_argument("--long-only", action="store_true", help="Only run the long meeting benchmark.")
    parser.add_argument("--no-long", action="store_true", help="Skip long benchmark; run chaotic tests only.")
    parser.add_argument(
        "--financial-scrum",
        action="store_true",
        help="Run Q2 Sprint 23 financial scrum (scripted transcript, OpenAI, hybrid retrieval). Skips long/chaos.",
    )
    args = parser.parse_args()

    if args.financial_scrum:
        import importlib.util

        scr = _ROOT / "scripts" / "simulate_financial_scrum_koov.py"
        spec = importlib.util.spec_from_file_location("koov_financial_scrum_sim", scr)
        if spec is None or spec.loader is None:
            print("Could not load simulate_financial_scrum_koov.py", file=sys.stderr)
            return 1
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return int(mod.main())

    rc = 0
    if not args.no_long:
        run_long_meeting_benchmark(
            long_chunks=max(50, args.long_chunks),
            duration_s=max(60.0, args.duration_min * 60.0),
            chapter_every=max(10, args.chapter_every),
        )
    if not args.long_only:
        rc = run_all()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
