"""
Meeting store: persist and load transcript + structured state per meeting.

- Store: append transcript chunks as they arrive; optionally save a snapshot of
  decisions, action items, open/postponed issues for quick load or display.
- Load: replay chunks into a context engine to restore full state, or read
  snapshot only for summaries.

Ensures we have proper storage so retrieval can serve answers even after
restart or for past meetings.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from koov.context_integrity_engine import ContextIntegrityEngine


def _tiger():
    """Tiger Data writer, or None when TIGER_DATABASE_URL is not configured. Never raises."""
    try:
        from koov.tiger_store import get_sink

        return get_sink()
    except Exception:
        return None


def chunk_content_fingerprint(speaker_id: str, text: str) -> str:
    """Stable short hash for dedupe / reprocessing (not cryptographic security)."""
    raw = f"{speaker_id}\0{text.strip()}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


@dataclass
class StoredChunk:
    """One persisted transcript line (legacy lines omit optional fields)."""

    ts: float
    speaker_id: str
    text: str
    chunk_id: int | None = None
    topic: str = "general"
    tags: list[str] = field(default_factory=list)
    speaker_display: str | None = None
    content_hash: str | None = None


def _default_base_dir() -> str:
    return os.environ.get("KOOV_MEETING_STORE_DIR", "koov_meetings")


class MeetingStore:
    """
    Per-meeting persistence: transcript chunks + optional snapshot.

    Directory layout:
      {base_dir}/{meeting_id}/chunks.jsonl   -- one JSON object per line (ts, speaker_id, text;
        optional chunk_id, topic, tags, speaker_display for retrieval/summary)
      {base_dir}/{meeting_id}/snapshot.json  -- last snapshot (decisions, action_items, open_questions, etc.)
    """

    def __init__(self, meeting_id: str, base_dir: Optional[str] = None):
        self.meeting_id = meeting_id
        self._base = Path(base_dir or _default_base_dir()).resolve()
        self._dir = self._base / meeting_id
        self._chunks_path = self._dir / "chunks.jsonl"
        self._snapshot_path = self._dir / "snapshot.json"

    def ensure_dir(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)

    def append_chunk(self, speaker_id: str, text: str, timestamp: Optional[float] = None) -> None:
        """Append one transcript chunk for later replay or retrieval (legacy three-field line)."""
        if not text or not text.strip():
            return
        self.ensure_dir()
        ts = timestamp if timestamp is not None else time.time()
        line = json.dumps({
            "ts": ts,
            "speaker_id": speaker_id,
            "text": text.strip(),
        }, ensure_ascii=False) + "\n"
        with open(self._chunks_path, "a", encoding="utf-8") as f:
            f.write(line)
        sink = _tiger()
        if sink:
            sink.record_chunk(self.meeting_id, speaker_id, text, ts,
                              content_hash=chunk_content_fingerprint(speaker_id, text))

    def append_enriched_chunk(
        self,
        speaker_id: str,
        text: str,
        timestamp: Optional[float] = None,
        *,
        chunk_id: int,
        topic: str = "general",
        tags: Optional[list[str]] = None,
        speaker_display: Optional[str] = None,
    ) -> None:
        """Append a chunk with engine-aligned metadata (chunk_id, topic, tags)."""
        if not text or not text.strip():
            return
        self.ensure_dir()
        ts = timestamp if timestamp is not None else time.time()
        payload: dict[str, Any] = {
            "ts": ts,
            "speaker_id": speaker_id,
            "text": text.strip(),
            "chunk_id": chunk_id,
            "topic": topic or "general",
            "tags": list(tags or []),
            "content_hash": chunk_content_fingerprint(speaker_id, text),
        }
        if speaker_display:
            payload["speaker_display"] = speaker_display
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        with open(self._chunks_path, "a", encoding="utf-8") as f:
            f.write(line)
        sink = _tiger()
        if sink:
            sink.record_chunk(
                self.meeting_id, speaker_id, text, ts,
                chunk_id=chunk_id, topic=payload["topic"], tags=payload["tags"],
                speaker_display=speaker_display, content_hash=payload["content_hash"],
            )

    def clear_for_new_session(self) -> None:
        """Remove persisted data for this meeting so this session starts fresh (no past-meeting bleed)."""
        if self._dir.exists():
            try:
                shutil.rmtree(self._dir)
            except OSError:
                pass

    def load_chunks(self) -> list[tuple[float, str, str]]:
        """Load all stored chunks as (timestamp, speaker_id, text). Replay into engine with ingest_chunk."""
        if not self._chunks_path.exists():
            return []
        out = []
        with open(self._chunks_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    ts = float(obj.get("ts", 0))
                    speaker = str(obj.get("speaker_id", "unknown"))
                    text = str(obj.get("text", ""))
                    out.append((ts, speaker, text))
                except (json.JSONDecodeError, ValueError):
                    continue
        return out

    def load_chunks_enriched(self) -> list[StoredChunk]:
        """Load all stored chunks including optional metadata (backward-compatible with legacy lines)."""
        if not self._chunks_path.exists():
            return []
        out: list[StoredChunk] = []
        with open(self._chunks_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    ts = float(obj.get("ts", 0))
                    speaker = str(obj.get("speaker_id", "unknown"))
                    text = str(obj.get("text", ""))
                    cid = obj.get("chunk_id")
                    chunk_id = int(cid) if cid is not None else None
                    topic = str(obj.get("topic") or "general")
                    raw_tags = obj.get("tags")
                    if isinstance(raw_tags, list):
                        tags = [str(t) for t in raw_tags]
                    else:
                        tags = []
                    sd = obj.get("speaker_display")
                    speaker_display = str(sd) if sd else None
                    ch = obj.get("content_hash")
                    content_hash = str(ch) if ch else None
                    out.append(
                        StoredChunk(
                            ts=ts,
                            speaker_id=speaker,
                            text=text,
                            chunk_id=chunk_id,
                            topic=topic,
                            tags=tags,
                            speaker_display=speaker_display,
                            content_hash=content_hash,
                        )
                    )
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue
        return out

    def save_snapshot(self, engine: ContextIntegrityEngine) -> None:
        """Save current structured state for quick load or display (no replay needed for summary)."""
        self.ensure_dir()
        decisions = engine.get_current_decisions()
        action_items = engine.get_action_items_structured()
        open_q = engine.get_open_questions_list()
        postponed = engine.get_postponed_issues_list()
        snapshot = {
            "meeting_id": self.meeting_id,
            "updated_at": time.time(),
            "decisions": [{"id": did, "value": val} for did, val in decisions],
            "action_items": [{"owner": o, "task": t, "deadline": d} for o, t, d in action_items],
            "open_questions": open_q,
            "postponed": postponed,
            "event_ids": engine.get_qa_event_ids(),
        }
        tmp_path = self._snapshot_path.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2, ensure_ascii=False)
        os.replace(str(tmp_path), str(self._snapshot_path))
        sink = _tiger()
        if sink:
            sink.record_snapshot(self.meeting_id, snapshot)
        return None

    def load_snapshot(self) -> Optional[dict[str, Any]]:
        """Load last saved snapshot (for display or lightweight Q&A). Returns None if missing."""
        if not self._snapshot_path.exists():
            return None
        try:
            with open(self._snapshot_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    def replay_into_engine(self, engine: ContextIntegrityEngine) -> int:
        """
        Replay all stored chunks into the given engine. Use after load_chunks to restore state.
        Returns number of chunks replayed.
        """
        chunks = self.load_chunks()
        for ts, speaker_id, text in chunks:
            engine.ingest_chunk(text, speaker_id=speaker_id, timestamp=ts)
        return len(chunks)

    @classmethod
    def clear_meeting(cls, meeting_id: str, base_dir: Optional[str] = None) -> None:
        """Remove persisted data for a meeting (e.g. to clear test data or avoid reusing past chunks)."""
        base = Path(base_dir or _default_base_dir())
        path = base / meeting_id
        if path.exists():
            try:
                shutil.rmtree(path)
            except OSError:
                pass

    @classmethod
    def list_meeting_ids(cls, base_dir: Optional[str] = None) -> list[str]:
        """List meeting IDs that have stored data."""
        base = Path(base_dir or _default_base_dir())
        if not base.exists():
            return []
        return [d.name for d in base.iterdir() if d.is_dir()]
