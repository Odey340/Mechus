"""
Team context store for Koov for Teams / Enterprise.

Meeting state remains isolated by meeting_id. This store is an explicit opt-in
team layer where dashboards can add shared notes or promote completed meetings
so future Koov sessions can use that team's history.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from koov.meeting_store import MeetingStore


def _default_base_dir() -> str:
    explicit = os.environ.get("KOOV_TEAM_STORE_DIR", "").strip()
    if explicit:
        return explicit
    notes_dir = os.environ.get("KOOV_NOTES_DIR", "koov_notes")
    return os.path.join(notes_dir, "team_context")


def normalize_team_id(team_id: str) -> str:
    tid = (team_id or "").strip()
    if not tid:
        raise ValueError("team_id is required")
    if len(tid) > 128:
        raise ValueError("team_id is too long")
    if not re.match(r"^[a-zA-Z0-9._-]+$", tid):
        raise ValueError("team_id may only contain letters, numbers, dots, underscores, and hyphens")
    return tid


def _truncate(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 24)].rstrip() + "\n...(truncated)"


def _safe_notes_filename_id(meeting_id: str) -> str:
    s = (meeting_id or "").strip()
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", s)
    return s[:200] if s else "session"


def _coerce_notes_path(path: str, notes_dir: str) -> str:
    raw = (path or "").strip()
    if not raw:
        return ""
    p = raw if os.path.isabs(raw) else os.path.abspath(raw)
    if os.path.isfile(p):
        return p
    if raw.startswith("/notes/"):
        rel = raw[len("/notes/") :].lstrip("/")
        cand = os.path.normpath(os.path.join(os.path.abspath(notes_dir), rel))
        if cand.startswith(os.path.abspath(notes_dir)) and os.path.isfile(cand):
            return cand
    base = os.path.basename(raw.replace("\\", "/"))
    if base:
        cand = os.path.join(os.path.abspath(notes_dir), base)
        if os.path.isfile(cand):
            return cand
    return ""


def _load_transcript_lines_from_notes(meeting_id: str, *, max_chars: int) -> list[str]:
    notes_dir = os.environ.get("KOOV_NOTES_DIR", "koov_notes")
    candidates: list[str] = []
    ws_path = os.path.join(notes_dir, f"worker-status-{meeting_id}.json")
    if os.path.isfile(ws_path):
        try:
            with open(ws_path, "r", encoding="utf-8") as f:
                ws = json.load(f)
            coerced = _coerce_notes_path(str(ws.get("transcript_path") or ""), notes_dir)
            if coerced:
                candidates.append(coerced)
        except (json.JSONDecodeError, OSError):
            pass
    candidates.append(os.path.join(notes_dir, f"{_safe_notes_filename_id(meeting_id)}.jsonl"))

    lines: list[str] = []
    total = 0
    seen: set[str] = set()
    for path in candidates:
        if not path or path in seen or not os.path.isfile(path):
            continue
        seen.add(path)
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                if not raw.strip():
                    continue
                try:
                    note = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(note, dict) or note.get("kind") != "transcript":
                    continue
                text = str(note.get("text") or "").strip()
                if not text:
                    continue
                lines.append(text)
                total += len(text) + 1
                if total >= max_chars:
                    return lines
    return lines


class TeamContextStore:
    """JSONL-backed team context store with no database dependency."""

    def __init__(self, team_id: str, base_dir: str | None = None):
        self.team_id = normalize_team_id(team_id)
        self._base = Path(base_dir or _default_base_dir())
        self._dir = self._base / self.team_id
        self._team_path = self._dir / "team.json"
        self._items_path = self._dir / "context.jsonl"

    def ensure_dir(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)

    def upsert_team(
        self,
        *,
        name: str = "",
        plan: str = "teams",
        created_by: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.ensure_dir()
        now = time.time()
        current = self.get_team() or {}
        payload = {
            "team_id": self.team_id,
            "name": (name or current.get("name") or self.team_id).strip(),
            "plan": (plan or current.get("plan") or "teams").strip(),
            "created_by": current.get("created_by") or (created_by or "").strip(),
            "created_at": current.get("created_at") or now,
            "updated_at": now,
            "metadata": {**(current.get("metadata") or {}), **(metadata or {})},
        }
        tmp = self._team_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._team_path)
        return payload

    def get_team(self) -> dict[str, Any] | None:
        if not self._team_path.exists():
            return None
        try:
            with open(self._team_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, OSError):
            return None

    def append_item(
        self,
        *,
        text: str,
        title: str = "",
        source_type: str = "manual",
        source_id: str = "",
        created_by: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        clean = (text or "").strip()
        if not clean:
            raise ValueError("text is required")
        self.ensure_dir()
        if self.get_team() is None:
            self.upsert_team(created_by=created_by)
        payload = {
            "id": str(uuid.uuid4()),
            "team_id": self.team_id,
            "source_type": (source_type or "manual").strip()[:64],
            "source_id": (source_id or "").strip()[:256],
            "title": (title or "").strip()[:256],
            "text": clean,
            "created_by": (created_by or "").strip()[:256],
            "created_at": time.time(),
            "metadata": metadata or {},
        }
        with open(self._items_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return payload

    def list_items(self, *, limit: int | None = None, source_type: str | None = None) -> list[dict[str, Any]]:
        if not self._items_path.exists():
            return []
        out: list[dict[str, Any]] = []
        wanted = (source_type or "").strip()
        with open(self._items_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if wanted and obj.get("source_type") != wanted:
                    continue
                out.append(obj)
        out.sort(key=lambda x: float(x.get("created_at") or 0), reverse=True)
        if limit is not None and limit > 0:
            return out[:limit]
        return out

    def add_meeting(
        self,
        meeting_id: str,
        *,
        title: str = "",
        created_by: str = "",
        final_summary: dict[str, Any] | None = None,
        max_transcript_chars: int = 16000,
    ) -> dict[str, Any]:
        mid = (meeting_id or "").strip()
        if not mid:
            raise ValueError("meeting_id is required")
        text = self._build_meeting_context_text(
            mid,
            title=title,
            final_summary=final_summary,
            max_transcript_chars=max_transcript_chars,
        )
        return self.append_item(
            text=text,
            title=title or f"Meeting {mid}",
            source_type="meeting",
            source_id=mid,
            created_by=created_by,
            metadata={"meeting_id": mid},
        )

    def list_meetings(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        return self.list_items(limit=limit, source_type="meeting")

    def format_for_prompt(self, *, max_chars: int = 6000) -> str:
        items = self.list_items(limit=20)
        blocks: list[str] = []
        total = 0
        for item in items:
            title = item.get("title") or item.get("source_id") or item.get("id")
            source = item.get("source_type") or "manual"
            body = str(item.get("text") or "").strip()
            if not body:
                continue
            block = f"[{source}: {title}]\n{body}"
            if total + len(block) + 2 > max_chars:
                room = max_chars - total - 32
                if room <= 120:
                    break
                block = _truncate(block, room)
            blocks.append(block)
            total += len(block) + 2
            if total >= max_chars:
                break
        return "\n\n".join(blocks).strip()

    def _build_meeting_context_text(
        self,
        meeting_id: str,
        *,
        title: str,
        final_summary: dict[str, Any] | None,
        max_transcript_chars: int,
    ) -> str:
        from koov.meeting_summary import read_final_summary

        summary = final_summary or read_final_summary(meeting_id) or {}
        store = MeetingStore(meeting_id)
        snapshot = store.load_snapshot() or {}
        chunks = store.load_chunks_enriched()

        parts: list[str] = [f"Meeting ID: {meeting_id}"]
        if title:
            parts.append(f"Title: {title}")

        summary_text = _summary_to_text(summary)
        if summary_text:
            parts.append("Final summary:\n" + summary_text)

        state_text = _snapshot_to_text(snapshot)
        if state_text:
            parts.append("Structured state:\n" + state_text)

        transcript_lines = []
        for ch in chunks:
            speaker = ch.speaker_display or ch.speaker_id or "unknown"
            transcript_lines.append(f"{speaker}: {ch.text}")
        if not transcript_lines:
            transcript_lines = _load_transcript_lines_from_notes(
                meeting_id,
                max_chars=max_transcript_chars,
            )
        if transcript_lines:
            parts.append("Transcript excerpts:\n" + _truncate("\n".join(transcript_lines), max_transcript_chars))

        if len(parts) == 1:
            raise ValueError(f"No stored meeting context found for meeting_id={meeting_id}")
        return "\n\n".join(parts)


def _summary_to_text(summary: dict[str, Any]) -> str:
    if not summary:
        return ""
    parts: list[str] = []
    executive = str(summary.get("executive_summary") or "").strip()
    if executive:
        parts.append(executive)
    for key, label in (
        ("decisions", "Decisions"),
        ("action_items", "Action items"),
        ("open_questions", "Open questions"),
        ("risks", "Risks"),
    ):
        lines = _listish_to_lines(summary.get(key))
        if lines:
            parts.append(f"{label}:\n" + "\n".join(lines))
    return "\n".join(parts).strip()


def _snapshot_to_text(snapshot: dict[str, Any]) -> str:
    if not snapshot:
        return ""
    parts: list[str] = []
    for key, label in (
        ("decisions", "Decisions"),
        ("action_items", "Action items"),
        ("open_questions", "Open questions"),
        ("postponed", "Postponed"),
    ):
        lines = _listish_to_lines(snapshot.get(key))
        if lines:
            parts.append(f"{label}:\n" + "\n".join(lines))
    return "\n".join(parts).strip()


def _listish_to_lines(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return [str(value)]
    lines: list[str] = []
    for item in value:
        if isinstance(item, str):
            if item.strip():
                lines.append("- " + item.strip())
        elif isinstance(item, dict):
            clean = {k: v for k, v in item.items() if v not in (None, "", [], {})}
            if clean:
                lines.append("- " + json.dumps(clean, ensure_ascii=False))
        else:
            lines.append("- " + str(item))
    return lines
