"""Tests for MeetingStore enriched lines, summary evidence builder, and final-report merge."""
from __future__ import annotations

import os
import tempfile
import unittest

from koov.context_integrity_engine import ContextIntegrityEngine, FinalReport
from koov.meeting_store import MeetingStore, StoredChunk
from koov.meeting_summary import build_summary_evidence_text, merge_final_report_into_summary


class TestMeetingStoreEnriched(unittest.TestCase):
    def test_append_enriched_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mid = "test-mid-1"
            store = MeetingStore(mid, base_dir=tmp)
            store.append_enriched_chunk(
                "alice",
                "We decided on June 1.",
                100.0,
                chunk_id=3,
                topic="timeline",
                tags=["decision"],
            )
            store.append_chunk("bob", "ok", 101.0)
            enriched = store.load_chunks_enriched()
            self.assertEqual(len(enriched), 2)
            self.assertEqual(enriched[0].chunk_id, 3)
            self.assertEqual(enriched[0].tags, ["decision"])
            self.assertEqual(enriched[0].topic, "timeline")
            self.assertIsNone(enriched[1].chunk_id)
            flat = store.load_chunks()
            self.assertEqual(len(flat), 2)
            self.assertEqual(flat[0][2], "We decided on June 1.")

    def test_load_legacy_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m1", "chunks.jsonl")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"ts": 1.0, "speaker_id": "x", "text": "hello"}\n')
            store = MeetingStore("m1", base_dir=tmp)
            e = store.load_chunks_enriched()[0]
            self.assertIsNone(e.chunk_id)
            self.assertEqual(e.tags, [])


class TestBuildSummaryEvidence(unittest.TestCase):
    def test_short_meeting_full_text(self) -> None:
        chunks = [
            StoredChunk(10.0, "a", "one", chunk_id=1, topic="general", tags=[]),
            StoredChunk(20.0, "b", "two", chunk_id=2, topic="general", tags=["decision"]),
        ]
        out = build_summary_evidence_text(chunks, max_chars=10000)
        self.assertIn("one", out)
        self.assertIn("two", out)
        self.assertLessEqual(len(out), 10000)

    def test_long_meeting_bounded_and_salient_first(self) -> None:
        chunks: list[StoredChunk] = []
        for i in range(200):
            tags = ["decision"] if i == 50 else []
            chunks.append(
                StoredChunk(
                    float(i),
                    "spk",
                    f"Utterance number {i} with some filler text.",
                    chunk_id=i + 1,
                    topic="general",
                    tags=tags,
                )
            )
        out = build_summary_evidence_text(chunks, max_chars=4000)
        self.assertLessEqual(len(out), 4100)
        self.assertIn("Salient tagged", out)
        self.assertIn("Utterance number 50", out)


class TestMergeFinalReport(unittest.TestCase):
    def test_fills_when_fallback(self) -> None:
        summary = {
            "executive_summary": "",
            "decisions": [],
            "action_items": [],
            "open_questions": [],
            "source": "fallback",
        }
        r = FinalReport(
            executive_summary="Exec from report.",
            final_decisions=["Use v2"],
            action_items_table=[("A", "Task", None, 1.0)],
            contradictions_identified=[],
            risks_identified=[],
            unresolved_questions=["Budget?"],
            speaker_alignment_matrix=[],
            timeline_decision_points=[],
            logic_inconsistencies=[],
            confidence_score=80,
            confidence_reasoning="ok",
        )
        out = merge_final_report_into_summary(summary, r)
        self.assertEqual(out["executive_summary"], "Exec from report.")
        self.assertTrue(out["decisions"])
        self.assertIn("Budget?", out["open_questions"])
        self.assertTrue(out.get("final_report_extras"))


if __name__ == "__main__":
    unittest.main()
