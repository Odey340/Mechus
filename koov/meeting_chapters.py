"""
Rolling chapter summaries persisted per meeting (tiered storage / warm layer).

Appends one line to ``chapters.jsonl`` under ``{base}/{meeting_id}/`` every N engine chunks.
"""

from __future__ import annotations

from koov.openai_compat import chat_completions_url as _koov_chat_url, supports_json_mode as _koov_json_ok

import json
import logging
import os
import re
import ssl
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:
    from koov.context_integrity_engine import ContextIntegrityEngine, TaggedChunk

log = logging.getLogger(__name__)

CHAPTERS_FILENAME = "chapters.jsonl"


@dataclass
class ChapterRecord:
    chapter_index: int
    ts_start: float
    ts_end: float
    summary: str
    chunk_ids: list[int]
    entities: list[str] = field(default_factory=list)


def chapters_path(base_dir: str | Path, meeting_id: str) -> Path:
    return Path(base_dir) / meeting_id / CHAPTERS_FILENAME


def load_chapters(base_dir: str | None, meeting_id: str) -> list[ChapterRecord]:
    base = base_dir or os.environ.get("KOOV_MEETING_STORE_DIR", "koov_meetings")
    path = chapters_path(base, meeting_id)
    if not path.is_file():
        return []
    out: list[ChapterRecord] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                out.append(
                    ChapterRecord(
                        chapter_index=int(obj.get("chapter_index", 0)),
                        ts_start=float(obj.get("ts_start", 0)),
                        ts_end=float(obj.get("ts_end", 0)),
                        summary=str(obj.get("summary", "")),
                        chunk_ids=[int(x) for x in (obj.get("chunk_ids") or []) if x is not None],
                        entities=[str(e) for e in (obj.get("entities") or [])],
                    )
                )
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
    return out


def format_chapters_for_prompt(records: Sequence[ChapterRecord], *, max_chars: int = 12000) -> str:
    if not records:
        return ""
    parts: list[str] = []
    for ch in records:
        ids = ch.chunk_ids
        head = ",".join(str(x) for x in ids[:5])
        tail = "…" if len(ids) > 5 else ""
        parts.append(f"### Chapter {ch.chapter_index} (chunk_ids {head}{tail})\n{ch.summary.strip()}")
    text = "\n\n".join(parts)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20].rsplit("\n", 1)[0] + "\n(…chapters truncated)"


def _extractive_summary(chunks: Sequence["TaggedChunk"], *, max_chars: int = 1500) -> str:
    lines = [f"[{c.speaker_id}] {c.text}" for c in chunks]
    text = "\n".join(lines)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 12] + "…"


def _llm_chapter_summary(
    combined_text: str,
    *,
    api_key: str,
    model: str,
    max_tokens: int = 400,
    timeout_s: float = 35.0,
) -> str | None:
    system = (
        "Summarize this slice of a meeting transcript so a teammate joining late could be brought up to speed.\n"
        "Capture, in 4–6 short sentences:\n"
        "  - the topics discussed (in order),\n"
        "  - any decisions landed on (with the value, e.g. 'launch July 25'),\n"
        "  - action items with owner and deadline if stated,\n"
        "  - open questions and any risks flagged,\n"
        "  - exact names, numbers, and dates as said.\n"
        "Be specific. Use names. No preamble, no markdown, no headings — plain sentences only.\n"
        "Never editorialize tone (no 'heated', 'chaotic'). Just WHAT and WHY."
    )
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": combined_text[:24000]},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.2,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        _koov_chat_url(),
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key.strip()}",
        },
        method="POST",
    )
    try:
        try:
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout_s, context=ctx) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log.debug("Chapter LLM summary failed: %s", exc)
        return None
    choices = payload.get("choices") or []
    if not choices:
        return None
    content = (choices[0].get("message") or {}).get("content") or ""
    return content.strip() or None


def _llm_chapter_digest(
    combined_text: str,
    *,
    api_key: str,
    model: str,
    max_tokens: int = 700,
    timeout_s: float = 35.0,
) -> dict | None:
    """One JSON call returning a chapter summary PLUS structured action items.

    Returns ``{"summary": str, "action_items": [{owner, task, deadline}]}`` or
    None on failure. The action items catch paraphrased commitments the regex
    extractor misses; the caller merges them into canonical state.
    """
    system = (
        "You read a slice of a meeting transcript and return STRICT JSON with two keys:\n"
        '  "summary": 4-6 plain sentences bringing a late teammate up to speed — topics in order, '
        "decisions (with values), open questions, risks, exact names/numbers/dates. No markdown, no preamble.\n"
        '  "action_items": a list of {"owner": str, "task": str, "deadline": str} for every commitment or '
        "task assigned in this slice. Include paraphrased ones (e.g. 'can you take the deck' -> owner who was "
        "asked, task 'take the deck'). owner = the person responsible (a name, or 'unassigned' if unclear). "
        "task = a short verb phrase. deadline = as stated, or empty string. If no action items, use [].\n"
        "Only include action items actually stated in THIS slice. Do not invent owners or deadlines."
    )
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": combined_text[:24000]},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.1,
            **({"response_format": {"type": "json_object"}} if _koov_json_ok() else {}),
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        _koov_chat_url(),
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key.strip()}",
        },
        method="POST",
    )
    try:
        try:
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout_s, context=ctx) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log.debug("Chapter LLM digest failed: %s", exc)
        return None
    choices = payload.get("choices") or []
    if not choices:
        return None
    content = (choices[0].get("message") or {}).get("content") or ""
    content = content.strip()
    if not content:
        return None
    try:
        obj = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    summary = str(obj.get("summary") or "").strip()
    raw_items = obj.get("action_items")
    items: list[dict] = []
    if isinstance(raw_items, list):
        for it in raw_items:
            if isinstance(it, dict) and (it.get("task") or "").strip():
                items.append(
                    {
                        "owner": str(it.get("owner") or "unassigned").strip() or "unassigned",
                        "task": str(it.get("task") or "").strip(),
                        "deadline": str(it.get("deadline") or "").strip(),
                    }
                )
    if not summary and not items:
        return None
    return {"summary": summary, "action_items": items}


def append_chapter(
    meeting_id: str,
    record: ChapterRecord,
    *,
    base_dir: str | None = None,
) -> str:
    base = Path(base_dir or os.environ.get("KOOV_MEETING_STORE_DIR", "koov_meetings"))
    out_dir = base / meeting_id
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / CHAPTERS_FILENAME
    payload: dict[str, Any] = {
        "chapter_index": record.chapter_index,
        "ts_start": record.ts_start,
        "ts_end": record.ts_end,
        "summary": record.summary,
        "chunk_ids": record.chunk_ids,
        "entities": record.entities,
        "written_at": time.time(),
    }
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
    log.info("Wrote chapter %s for meeting %s", record.chapter_index, meeting_id)
    return str(path)


def maybe_roll_chapter(
    meeting_id: str,
    engine: "ContextIntegrityEngine",
    *,
    every_n_chunks: int,
    base_dir: str | None = None,
    api_key: str | None = None,
    chat_model: str = "gpt-4o-mini",
    chapter_llm_max_tokens: int = 400,
    chunk_count: int | None = None,
) -> None:
    """
    If len(tagged_chunks) is a positive multiple of every_n_chunks, summarize the last N chunks and append.

    ``chunk_count`` pins the boundary to summarize at (the tagged-chunk count
    when the roll was triggered). This keeps the window stable when the roll
    runs on a background thread and the live engine has since grown.
    """
    if every_n_chunks <= 0:
        return
    tagged = engine._tagged_chunks  # noqa: SLF001
    n = chunk_count if chunk_count is not None else len(tagged)
    n = min(n, len(tagged))
    if n == 0 or n % every_n_chunks != 0:
        return
    window = tagged[n - every_n_chunks : n]
    ts_vals = []
    for c in window:
        idx = c.chunk_id - 1
        ts_list = engine._chunk_timestamps  # noqa: SLF001
        if 0 <= idx < len(ts_list):
            ts_vals.append(ts_list[idx])
    if not ts_vals:
        ts_vals = [time.time()]
    ts_start = min(ts_vals)
    ts_end = max(ts_vals)
    combined = "\n".join(f"[{c.speaker_id}] {c.text}" for c in window)
    chapter_index = n // every_n_chunks - 1
    summary: str
    if api_key and api_key.strip():
        digest = _llm_chapter_digest(
            combined,
            api_key=api_key,
            model=chat_model,
            max_tokens=max(chapter_llm_max_tokens, 500),
        )
        if digest is not None:
            summary = digest.get("summary") or _extractive_summary(window)
            # Feed LLM-extracted action items back into canonical state so long
            # meetings capture paraphrased commitments the regex extractor misses.
            items = digest.get("action_items") or []
            if items and hasattr(engine, "merge_extracted_action_items"):
                try:
                    added = engine.merge_extracted_action_items(
                        items, chunk_id=window[-1].chunk_id, ts=ts_end
                    )
                    if added:
                        log.info(
                            "Chapter %s: merged %d LLM action item(s) into canonical state",
                            chapter_index,
                            added,
                        )
                except Exception as exc:  # noqa: BLE001
                    log.debug("Action-item merge failed: %s", exc)
        else:
            summary = _extractive_summary(window)
    else:
        summary = _extractive_summary(window)
    record = ChapterRecord(
        chapter_index=chapter_index,
        ts_start=ts_start,
        ts_end=ts_end,
        summary=summary,
        chunk_ids=[c.chunk_id for c in window],
        entities=[],
    )
    append_chapter(meeting_id, record, base_dir=base_dir)
