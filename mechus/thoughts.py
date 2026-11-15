"""Thread-safe reasoning trace for a single Mechus session.

ThoughtStream collects agent reasoning entries (thinking, retrieval context, planned
speech) during a live meeting and exposes them as a canonical list for GET /api/status.

Canonical entry shape (omits null optional fields):
  {
    "ts": 1710000004,           # Unix seconds (int)
    "kind": "thinking",         # thinking | connection | planned | context
    "text": "...",              # main reasoning body
    "planned_speech": "...",    # optional — what Mechus is about to say
    "connections": ["..."]      # optional — short labels for linked prior context
  }
"""

from __future__ import annotations

import time
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any

_MAX_ENTRIES = 80
_DEDUPE_WINDOW_S = 1.0   # same ts-second + same text prefix → skip
_DEDUPE_PREFIX_LEN = 120


@dataclass
class ThoughtEntry:
    ts: int
    kind: str
    text: str
    planned_speech: str | None = None
    connections: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "ts": self.ts,
            "kind": self.kind,
            "text": self.text,
        }
        if self.planned_speech:
            d["planned_speech"] = self.planned_speech
        if self.connections:
            d["connections"] = self.connections
        return d


class ThoughtStream:
    """Append-only, capped, deduplicated stream of agent reasoning entries."""

    VALID_KINDS = frozenset({"thinking", "connection", "planned", "context"})

    def __init__(self, max_entries: int = _MAX_ENTRIES) -> None:
        self._lock = threading.Lock()
        self._entries: deque[ThoughtEntry] = deque(maxlen=max_entries)

    def append(
        self,
        kind: str,
        text: str,
        *,
        planned_speech: str | None = None,
        connections: list[str] | None = None,
    ) -> None:
        """Append a new thought, skipping empty text and near-duplicate entries."""
        text = (text or "").strip()
        if not text:
            return
        if kind not in self.VALID_KINDS:
            kind = "thinking"
        ps = (planned_speech or "").strip() or None
        conns = [c for c in (connections or []) if c and c.strip()] or None
        ts = int(time.time())
        prefix = text[:_DEDUPE_PREFIX_LEN]

        with self._lock:
            # Dedupe: same second + same text prefix → no-op
            for entry in reversed(self._entries):
                if abs(entry.ts - ts) > _DEDUPE_WINDOW_S:
                    break
                if entry.kind == kind and entry.text[:_DEDUPE_PREFIX_LEN] == prefix:
                    return
            self._entries.append(
                ThoughtEntry(ts=ts, kind=kind, text=text, planned_speech=ps, connections=conns)
            )

    def snapshot(self) -> list[dict[str, Any]]:
        """Return all entries as a list of canonical dicts, oldest first."""
        with self._lock:
            return [e.to_dict() for e in self._entries]

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
