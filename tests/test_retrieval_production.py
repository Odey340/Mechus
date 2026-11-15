"""
Regression tests for BM25+tag retrieval, recap chapter prefix, and summary evidence with chapters.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from koov.context_integrity_engine import ContextIntegrityEngine
from koov.meeting_chapters import append_chapter, ChapterRecord
from koov.meeting_store import StoredChunk
from koov.meeting_summary import build_summary_evidence_text
from koov.retrieval import retrieve_for_question


def _ingest(engine: ContextIntegrityEngine, lines: list[tuple[str, str]]) -> None:
    for spk, text in lines:
        engine.ingest_chunk(text, speaker_id=spk, timestamp=0.0)


def test_retrieve_prefers_keyword_overlap_and_bm25() -> None:
    eng = ContextIntegrityEngine(meeting_id="t1")
    _ingest(
        eng,
        [
            ("A", "We discussed the weather and lunch."),
            ("B", "The Q4 budget freeze is locked for July."),
            ("A", "Parking was full today."),
        ],
    )
    bundle = retrieve_for_question(
        "What is the budget status?",
        context_engine=eng,
        memory=None,
        max_chars=4000,
        retrieval_mode="lexical",
    )
    assert bundle.meta.get("retrieval_mode") == "lexical"
    assert bundle.meta.get("retrieval_ms", 0) >= 0
    assert "budget" in (bundle.formatted_context or "").lower()
    assert bundle.event_ids


def test_recap_includes_chapter_block_when_present(tmp_path: Path) -> None:
    mid = "meet_ch"
    base = str(tmp_path)
    rec = ChapterRecord(
        chapter_index=0,
        ts_start=0.0,
        ts_end=60.0,
        summary="Team agreed on API v2 rollout.",
        chunk_ids=[1, 2],
        entities=[],
    )
    append_chapter(mid, rec, base_dir=base)

    eng = ContextIntegrityEngine(meeting_id=mid)
    _ingest(
        eng,
        [
            ("A", "Hello everyone."),
            ("B", "We will ship next week."),
        ],
    )

    old = os.environ.get("KOOV_MEETING_STORE_DIR")
    try:
        os.environ["KOOV_MEETING_STORE_DIR"] = base
        b2 = retrieve_for_question(
            "recap the meeting",
            context_engine=eng,
            memory=None,
            max_chars=8000,
        )
    finally:
        if old is None:
            os.environ.pop("KOOV_MEETING_STORE_DIR", None)
        else:
            os.environ["KOOV_MEETING_STORE_DIR"] = old

    assert "Rolling chapter" in b2.formatted_context or "chapter" in b2.formatted_context.lower()
    assert "API" in b2.formatted_context or "rollout" in b2.formatted_context.lower()


def test_build_summary_evidence_includes_chapters(tmp_path: Path) -> None:
    mid = "m_ev"
    base = str(tmp_path)
    p = Path(base) / mid / "chapters.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {
            "chapter_index": 0,
            "ts_start": 0,
            "ts_end": 10,
            "summary": "Discussed hiring plan.",
            "chunk_ids": [1],
            "entities": [],
        }
    )
    p.write_text(line + "\n", encoding="utf-8")

    chunks = [
        StoredChunk(ts=0.0, speaker_id="A", text="x " * 2000, chunk_id=1, tags=["decision"]),
        StoredChunk(ts=1.0, speaker_id="B", text="y " * 2000, chunk_id=2, tags=[]),
    ]
    out = build_summary_evidence_text(
        chunks,
        max_chars=5000,
        meeting_id=mid,
        chapters_base_dir=base,
    )
    assert "Rolling chapter" in out
    assert "hiring" in out.lower()
