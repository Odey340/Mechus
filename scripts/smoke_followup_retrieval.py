#!/usr/bin/env python3
"""
Retrieval-only smoke test for dialog follow-ups (no STT/TTS/LLM).

Ingests a fixed synthetic meeting, then simulates two-turn flows using the same
dialog_memory + retrieve_for_question wiring as MechusAgent (expanded query for turn 2).

Usage:
  python3 scripts/smoke_followup_retrieval.py
"""

from __future__ import annotations

import json
import os
import sys

_script_dir = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_script_dir)
if _root not in sys.path:
    sys.path.insert(0, _root)

from mechus.context_integrity_engine import ContextIntegrityEngine
from mechus.dialog_memory import (
    build_retrieval_query,
    extract_salient_terms,
    merge_salient,
)
from mechus.memory import ConversationMemory
from mechus.retrieval import retrieve_for_question


def _ingest_meeting(eng: ContextIntegrityEngine, t: float = 0.0) -> None:
    lines: list[tuple[str, str]] = [
        ("Alice", "We may need to delay the NIH rollout to Q3 due to compliance blockers."),
        ("Bob", "The three main blockers are funding, security review, and unclear ownership."),
        ("Alice", "Sprint 24 follow-ups are owned by Priya; anything on H1B should go to legal."),
        ("Carol", "Supabase persistence is a concern for PII — we need encryption at rest reviewed."),
        ("Dave", "We decided API versioning stays on v2 for the launch unless security objects."),
        ("Eve", "Open action items: finalize the runbook, assign an owner for the dashboard, and confirm the launch window."),
        ("Frank", "Launch risks include vendor slip, load test gaps, and holiday freeze."),
    ]
    step = 0.5
    for spk, text in lines:
        eng.ingest_chunk(text, speaker_id=spk, timestamp=t)
        t += step


def _kw_hits(text: str, keywords: list[str]) -> list[str]:
    low = text.lower()
    return [k for k in keywords if k.lower() in low]


def _run_turn(
    eng: ContextIntegrityEngine,
    mem: ConversationMemory,
    cmd: str,
    *,
    retrieval_query: str | None = None,
    salient_terms_used: list[str] | None = None,
) -> dict:
    b = retrieve_for_question(
        cmd,
        retrieval_query=retrieval_query,
        salient_terms_used=salient_terms_used,
        context_engine=eng,
        memory=mem,
        max_chars=6000,
        retrieval_extra_meta={"smoke": True},
    )
    ctx = (b.formatted_context or "") + "\n" + (b.memory_text or "")
    return {
        "cmd": cmd,
        "retrieval_effective_query": b.meta.get("retrieval_effective_query"),
        "vague_followup": b.meta.get("vague_followup"),
        "retrieval_query_expanded": b.meta.get("retrieval_query_expanded"),
        "chunk_ids": b.meta.get("selected_chunk_ids") or b.event_ids,
        "context_sample": ctx[:900],
    }


def main() -> int:
    mid = "smoke_followup_eval"
    eng = ContextIntegrityEngine(meeting_id=mid)
    _ingest_meeting(eng)

    _mem_kw = dict(max_chars=10000, recent_chars=2500)

    scenarios = [
        {
            "name": "NIH anaphora",
            "t1": "What did Alice say about the NIH rollout?",
            "t2": "what about that?",
            "mechus_after_t1": "Alice said the NIH rollout might slip to Q3 due to compliance blockers.",
            "expect2": ["nih", "alice", "rollout"],
        },
        {
            "name": "Three blockers → first one",
            "t1": "What were the three blockers?",
            "t2": "tell me more about the first one",
            "mechus_after_t1": "The blockers were funding, security review, and unclear ownership.",
            "expect2": ["funding", "blocker"],
        },
        {
            "name": "Sprint 24 → H1B same framing",
            "t1": "Who owns Sprint 24 follow-ups?",
            "t2": "same thing for H1B?",
            "mechus_after_t1": "Sprint 24 follow-ups are owned by Priya.",
            "expect2": ["h1b", "sprint", "priya"],
        },
    ]

    print("=== smoke_followup_retrieval (synthetic transcript) ===\n")

    for sc in scenarios:
        print(f"--- {sc['name']} ---")
        mem = ConversationMemory(**_mem_kw)

        r1 = _run_turn(eng, mem, sc["t1"], retrieval_query=None)
        print("Turn 1:", sc["t1"])
        print("  expanded:", r1["retrieval_query_expanded"], "vague:", r1["vague_followup"])

        salient = merge_salient([], extract_salient_terms(sc["t1"]))
        merged = merge_salient(salient, extract_salient_terms(sc["t2"]))
        rq = build_retrieval_query(
            sc["t2"],
            last_user=sc["t1"],
            last_mechus=sc["mechus_after_t1"],
            salient_terms=merged,
        )
        r2 = _run_turn(eng, mem, sc["t2"], retrieval_query=rq, salient_terms_used=merged)
        hits = _kw_hits(r2["context_sample"], sc["expect2"])
        ok = len(hits) >= 1
        print("Turn 2:", sc["t2"])
        print("  effective_query (first 200 chars):", (r2["retrieval_effective_query"] or "")[:200].replace("\n", " | "))
        print("  keyword hits:", hits, "OK" if ok else "WEAK")
        if not ok:
            print("  context_sample:", r2["context_sample"][:400])
        print()

    eval_path = os.path.join(_root, "evals", "followup_eval_cases.json")
    if os.path.isfile(eval_path):
        with open(eval_path, encoding="utf-8") as f:
            cases = json.load(f)
        print(f"Loaded {len(cases)} cases from evals/followup_eval_cases.json (see evals/LIVE_EVAL_CHECKLIST.md for live steps).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
