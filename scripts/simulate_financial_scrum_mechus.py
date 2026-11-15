#!/usr/bin/env python3
"""
Financial Scrum — same code path as a live Mechus meeting (no script-side LLM defaults).

Uses only `MechusConfig.from_env()` — configure `.env` like production:
  - MECHUS_OPENAI_API_KEY (or OPENAI_API_KEY)
  - MECHUS_LLM_BACKEND=openai
  - MECHUS_RETRIEVAL_MODE=lexical | hybrid | auto  (auto = conditional embedding rerank)
  - Compare runs:  python3 scripts/simulate_financial_scrum_mechus.py --ab
  - Optional: MECHUS_CHAPTER_EVERY_N_CHUNKS, MECHUS_EMBEDDING_MODEL, MECHUS_OPENAI_CHAT_MODEL, …

Feeds the scripted Q2 Sprint 23 meeting into ContextIntegrityEngine + MeetingStore,
chapter rollups (OpenAI when configured), hybrid retrieval, OpenAI Chat Completions
responses, and memory updates after each Q&A (like the agent).

  cd mechusdemo
  python3 scripts/simulate_financial_scrum_mechus.py
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Load .env if present (same as test_financial_scrum.py)
_env_path = _ROOT / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# Bridge common env name → Mechus (same as many deployments)
if os.environ.get("OPENAI_API_KEY") and not os.environ.get("MECHUS_OPENAI_API_KEY"):
    os.environ["MECHUS_OPENAI_API_KEY"] = os.environ["OPENAI_API_KEY"]

from mechus.config import MechusConfig
from mechus.context_integrity_engine import ContextIntegrityEngine
from mechus.meeting_chapters import maybe_roll_chapter
from mechus.meeting_store import MeetingStore
from mechus.memory import ConversationMemory
from mechus.responder import HybridResponder, OpenAIResponder, ResponseInput, RuleResponder
from mechus.retrieval import retrieve_for_question
from mechus.simulations.financial_scrum_transcript import (
    FINANCIAL_SCRUM_UTTERANCES,
    SPEAKER_LABELS,
    build_financial_scrum_timeline,
)


def _fmt_ms(ms: float) -> str:
    if ms >= 1000:
        return f"{ms / 1000:.3f}s"
    return f"{ms:.3f}ms"


def _load_config() -> MechusConfig:
    return MechusConfig.from_env()


def _validate_live_meeting_config(config: MechusConfig) -> str | None:
    """Return error message if config cannot mirror a real OpenAI-backed session."""
    key = (config.openai_api_key or "").strip()
    if not key:
        return "Set MECHUS_OPENAI_API_KEY (or OPENAI_API_KEY) in the environment or .env."
    if config.llm_backend != "openai":
        return (
            "MECHUS_LLM_BACKEND must be 'openai' for this simulation (same as a live meeting with Chat Completions). "
            f"Currently: {config.llm_backend!r}. Set MECHUS_LLM_BACKEND=openai in .env."
        )
    return None


def _build_responder(config: MechusConfig) -> HybridResponder | RuleResponder:
    rules = RuleResponder(max_response_words=config.max_response_words)
    if config.llm_backend == "openai" and config.openai_api_key:
        openai_resp = OpenAIResponder(
            api_key=config.openai_api_key,
            model=config.openai_chat_model or "gpt-4o-mini",
            max_response_words=config.max_response_words,
            openai_max_tokens=config.response_openai_max_tokens,
            openai_max_tokens_list=config.response_openai_max_tokens_list,
            timeout_s=config.response_openai_timeout_s,
            prompt_max_chars=config.response_prompt_max_chars,
            json_mode=config.response_json_mode,
            stream_enabled=config.response_stream_enabled,
            temperature_override=config.response_temperature,
            temp_meeting=config.response_temp_meeting,
        )
        return HybridResponder(primary=openai_resp, fallback=rules)
    return rules


def _ingest_one_utterance(
    config: MechusConfig,
    engine: ContextIntegrityEngine,
    store: MeetingStore | None,
    key: str,
    text: str,
    ts: float,
) -> None:
    """Ingest a single transcript line (same as agent path)."""
    label = SPEAKER_LABELS.get(key, key)
    short = key.upper()
    print(f"  [{short}] {text}")
    _mu, _end, tagged = engine.ingest_chunk(text, speaker_id=label, timestamp=ts)
    if store is not None:
        if tagged is not None:
            store.append_enriched_chunk(
                label,
                text,
                ts,
                chunk_id=tagged.chunk_id,
                topic=tagged.topic,
                tags=tagged.tags,
            )
        else:
            store.append_chunk(speaker_id=label, text=text, timestamp=ts)
        maybe_roll_chapter(
            engine.meeting_id,
            engine,
            every_n_chunks=config.chapter_every_n_chunks,
            base_dir=str(store._base),
            api_key=config.openai_api_key,
            chat_model=config.openai_chat_model or "gpt-4o-mini",
            chapter_llm_max_tokens=config.chapter_llm_max_tokens,
        )


def _ask_mechus(
    question: str,
    *,
    label: str,
    config: MechusConfig,
    engine: ContextIntegrityEngine,
    memory: ConversationMemory,
    responder: HybridResponder | RuleResponder,
    meeting_now_ts: float | None = None,
) -> tuple[str, float, float, dict]:
    """Retrieve + respond; return (answer, llm_wall_ms, total_wall_ms, retrieval_meta)."""
    t_all0 = time.perf_counter()
    bundle = retrieve_for_question(
        question,
        context_engine=engine,
        memory=memory,
        recent_transcript="",
        max_chars=config.retrieval_max_chars,
        general_max_chars=config.retrieval_general_max_chars,
        retrieval_mode=config.retrieval_mode,
        bm25_weight=config.retrieval_bm25_weight,
        tag_weight=config.retrieval_tag_weight,
        embed_weight=config.retrieval_embed_weight,
        embed_rerank_top_k=config.retrieval_embed_rerank_top_k,
        openai_api_key=config.openai_api_key,
        embedding_model=config.embedding_model,
        retrieval_auto_lexical_gap_min=config.retrieval_auto_lexical_gap_min,
        retrieval_auto_top_floor_hybrid=config.retrieval_auto_top_floor_hybrid,
        query_embed_cache_max=config.retrieval_query_embed_cache_max,
        meeting_now_ts=meeting_now_ts,
    )
    t0 = time.perf_counter()
    resp = responder.respond(
        ResponseInput(
            wake_phrase=config.wake_phrase,
            wake_phrases=config.wake_phrases,
            request_text=question,
            context_text=bundle.formatted_context or "",
            memory_text=bundle.memory_text,
            context_chunks=getattr(bundle, "chunks", None) or [],
            intent=getattr(bundle, "intent", None) or "",
            state=getattr(bundle, "state", None) or {},
            retrieval_meta=bundle.meta,
        )
    ).strip()
    llm_ms = (time.perf_counter() - t0) * 1000
    total_ms = (time.perf_counter() - t_all0) * 1000
    meta = dict(bundle.meta)
    meta["retrieval_label"] = label
    # Same as agent: persist Q&A into dialog memory for follow-up questions
    memory.add_segment(f"User: {question}")
    memory.add_segment(f"Mechus: {resp}")
    return resp, llm_ms, total_ms, meta


def _rate_answer(question: str, answer: str) -> tuple[float, str]:
    """Lightweight heuristic score (same spirit as test_financial_scrum.rate_response)."""
    a = (answer or "").lower()
    notes: list[str] = []
    score = 0.0
    if "error" in a and ("openai" in a or "api" in a):
        return 0.0, "API error in answer"
    score += 2.0
    notes.append("answered")
    nums = len(re.findall(r"\d[\d,]*", answer))
    if nums >= 3:
        score += 2.0
        notes.append(f"numbers~{nums}")
    elif nums >= 1:
        score += 1.0
        notes.append(f"some numbers ({nums})")
    wc = len(answer.split())
    if wc >= 40:
        score += 2.0
        notes.append(f"substantive ({wc} words)")
    elif wc >= 15:
        score += 1.0
        notes.append(f"brief ({wc} words)")
    if "evt_" not in a:
        score += 2.0
        notes.append("no evt_ leak")
    else:
        notes.append("possible id leak")
    return min(score, 10.0), " | ".join(notes)


def _run_one_financial_scrum(
    config: MechusConfig,
    *,
    trial_label: str,
    print_top_banner: bool,
) -> tuple[list[tuple[str, str, float, float, float, dict, float, str]], dict[str, float | int]]:
    """
    One full ingest + Mechus Q&A pass. Returns (results rows, stats) and prints timing summary.
    """
    tmp = tempfile.mkdtemp(prefix="mechus_fin_scrum_")
    prev_store = os.environ.get("MECHUS_MEETING_STORE_DIR")
    os.environ["MECHUS_MEETING_STORE_DIR"] = tmp

    meeting_id = "fin_scrum_q2_sprint23"
    engine = ContextIntegrityEngine(meeting_id=meeting_id)
    store = MeetingStore(meeting_id, base_dir=tmp)
    memory = ConversationMemory(
        max_chars=config.memory_max_chars,
        recent_chars=config.memory_recent_chars,
    )
    responder = _build_responder(config)
    if not isinstance(responder, HybridResponder):
        print("\n  ERROR: OpenAI responder not active — check MECHUS_LLM_BACKEND=openai and API key.\n")
        if prev_store is None:
            os.environ.pop("MECHUS_MEETING_STORE_DIR", None)
        else:
            os.environ["MECHUS_MEETING_STORE_DIR"] = prev_store
        return [], {"error": 1}

    if print_top_banner:
        print("\n" + "=" * 80)
        print("  FINANCIAL SCRUM — Live meeting API path (MechusConfig.from_env only)")
        print("=" * 80)

    print(f"\n  --- Trial: {trial_label} ---")
    print(f"\n  Meeting ID           : {meeting_id}")
    print(f"  Store dir            : {tmp}")
    print(f"  MECHUS_LLM_BACKEND     : {config.llm_backend}")
    print(f"  MECHUS_OPENAI_CHAT_MODEL: {config.openai_chat_model or 'gpt-4o-mini'}")
    print(f"  response max_tokens  : {config.response_openai_max_tokens}")
    print(f"  MECHUS_RETRIEVAL_MODE  : {config.retrieval_mode}")
    print(f"  MECHUS_EMBEDDING_MODEL : {config.embedding_model}")
    print(f"  chapter every N chunks: {config.chapter_every_n_chunks}")
    print(f"  Utterances           : {len(FINANCIAL_SCRUM_UTTERANCES)}")
    timeline = build_financial_scrum_timeline()
    n_mechus = sum(1 for step in timeline if step[0] == "mechus")
    print(f"  Timeline steps       : {len(timeline)} (Mechus turns: {n_mechus})")
    print()

    print("--- Meeting simulation (dialogue → Mechus → dialogue → …) ---\n")
    ts = 0.0
    seconds_per_segment = 10.0
    ingest_only_ms = 0.0
    sim_t0 = time.perf_counter()

    results: list[tuple[str, str, float, float, float, dict, float, str]] = []
    total_llm_ms = 0.0
    total_retrieval_meta_ms = 0.0
    total_e2e_ms = 0.0

    for step in timeline:
        kind = step[0]
        if kind == "section":
            title = step[1]
            print(f"\n  {'=' * 72}\n    {title}\n  {'=' * 72}\n")
            continue
        if kind == "utterance":
            _k, text = step[1], step[2]
            t_ing = time.perf_counter()
            _ingest_one_utterance(config, engine, store, _k, text, ts)
            ingest_only_ms += (time.perf_counter() - t_ing) * 1000
            ts += seconds_per_segment
            continue
        if kind == "mechus":
            label, q = step[1], step[2]
            print(f"\n  {'▼' * 28}")
            print(f"  Mechus  [{label}]")
            print(f"  Q: {q}")
            if label == "capstone-full-meeting":
                print(
                    "  » Designed to need both opening (Sprint 23 velocity/burn) and "
                    "closing (freeze, go-live, vendor) context in retrieval."
                )
            answer, llm_ms, total_ms, meta = _ask_mechus(
                q,
                label=label,
                config=config,
                engine=engine,
                memory=memory,
                responder=responder,
                meeting_now_ts=ts,
            )
            rms = float(meta.get("retrieval_ms") or 0)
            total_retrieval_meta_ms += rms
            total_llm_ms += llm_ms
            total_e2e_ms += total_ms
            score, rating_notes = _rate_answer(q, answer)
            results.append((label, q, rms, llm_ms, total_ms, meta, score, rating_notes))
            preview = answer.replace("\n", " ")[:400]
            if len(answer) > 400:
                preview += "..."
            print(f"  Mechus: {preview}")
            print(
                f"  retrieval_ms: {rms:.2f}  |  LLM_ms: {llm_ms:.1f}  |  "
                f"total_ms: {total_ms:.1f}  |  embed_rerank={meta.get('embed_rerank_used')}  |  score: {score:.1f}/10"
            )
            print(f"  rating: {rating_notes}")
            print(f"  {'▲' * 28}\n")
            continue

    sim_wall_s = time.perf_counter() - sim_t0
    n_chunks = len(engine._tagged_chunks)  # noqa: SLF001
    print("--- Timing (simulation) ---\n")
    print(f"  Utterance ingest only : {_fmt_ms(ingest_only_ms)}")
    print(f"  Full run wall clock   : {_fmt_ms(sim_wall_s * 1000)} (includes all Mechus API calls)")
    print(f"  Tagged chunks in engine: {n_chunks}")
    print()

    nq = len(results)
    avg_score = sum(r[6] for r in results) / nq if nq else 0
    print("=" * 80)
    print(f"  SUMMARY  ({trial_label})")
    print("=" * 80)
    print(f"  Questions              : {nq}")
    print(f"  Avg retrieval_ms       : {total_retrieval_meta_ms / nq:.2f} ms")
    print(f"  Avg LLM wall_ms        : {total_llm_ms / nq:.1f} ms")
    print(f"  Avg total_ms (e2e)     : {total_e2e_ms / nq:.1f} ms")
    print(f"  Total LLM time         : {total_llm_ms / 1000:.2f} s")
    print(f"  Total end-to-end time  : {total_e2e_ms / 1000:.2f} s")
    print(f"  Avg heuristic score    : {avg_score:.1f} / 10")
    cap = next((r for r in results if r[0] == "capstone-full-meeting"), None)
    if cap:
        print(f"  Capstone score (early+late context): {cap[6]:.1f} / 10")
    print()

    if prev_store is None:
        os.environ.pop("MECHUS_MEETING_STORE_DIR", None)
    else:
        os.environ["MECHUS_MEETING_STORE_DIR"] = prev_store

    try:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
    except Exception:
        pass

    stats = {
        "nq": nq,
        "avg_retrieval_ms": total_retrieval_meta_ms / nq if nq else 0.0,
        "avg_llm_ms": total_llm_ms / nq if nq else 0.0,
        "avg_total_ms": total_e2e_ms / nq if nq else 0.0,
        "avg_score": avg_score,
        "n_chunks": n_chunks,
    }
    return results, stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Financial Scrum simulation (same path as live Mechus).")
    parser.add_argument(
        "--ab",
        action="store_true",
        help="Run two isolated trials: MECHUS_RETRIEVAL_MODE=lexical vs auto (side-by-side aggregates).",
    )
    args = parser.parse_args()

    config = _load_config()
    err = _validate_live_meeting_config(config)
    if err:
        print(f"\n  ERROR: {err}\n")
        return 1

    if not args.ab and config.retrieval_mode not in ("hybrid", "auto"):
        print(
            "\n  NOTE: MECHUS_RETRIEVAL_MODE is not 'hybrid' or 'auto' — embedding rerank runs only in those modes. "
            "Use --ab to compare lexical vs auto, or set MECHUS_RETRIEVAL_MODE in .env.\n"
        )

    if args.ab:
        cfg_lex = replace(config, retrieval_mode="lexical")
        cfg_auto = replace(config, retrieval_mode="auto")
        print("\n" + "=" * 80)
        print("  A/B: lexical  vs  auto (conditional hybrid)")
        print("=" * 80)
        r1, s1 = _run_one_financial_scrum(cfg_lex, trial_label="lexical", print_top_banner=False)
        if s1.get("error"):
            return 1
        r2, s2 = _run_one_financial_scrum(cfg_auto, trial_label="auto", print_top_banner=False)
        if s2.get("error"):
            return 1
        print("=" * 80)
        print("  A/B COMPARISON (aggregate)")
        print("=" * 80)
        print(f"  {'metric':<22} {'lexical':>14} {'auto':>14}")
        print(f"  {'avg retrieval_ms':<22} {s1['avg_retrieval_ms']:>14.2f} {s2['avg_retrieval_ms']:>14.2f}")
        print(f"  {'avg total_ms (e2e)':<22} {s1['avg_total_ms']:>14.1f} {s2['avg_total_ms']:>14.1f}")
        print(f"  {'avg heuristic score':<22} {s1['avg_score']:>14.1f} {s2['avg_score']:>14.1f}")
        print(f"  {'tagged chunks':<22} {s1['n_chunks']:>14} {s2['n_chunks']:>14}")
        print()
        ok = s1["avg_score"] >= 4.0 and s2["avg_score"] >= 4.0
        return 0 if ok else 1

    results, stats = _run_one_financial_scrum(config, trial_label=config.retrieval_mode, print_top_banner=True)
    if stats.get("error"):
        return 1
    return 0 if stats["avg_score"] >= 4.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())