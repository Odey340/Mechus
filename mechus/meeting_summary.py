"""
End-of-meeting summary: full transcript → LLM → executive summary + action items for dashboard.

Used when the meeting ends (POST /api/stop). The worker (or API server for in-process sessions)
calls generate_final_summary() and writes the result to {meeting_store_dir}/{meeting_id}/final_summary.json.
The API server includes it in the stop response so the dashboard can persist and display it.
"""

from __future__ import annotations

from mechus.openai_compat import chat_completions_url as _mechus_chat_url, supports_json_mode as _mechus_json_ok

import hashlib
import json
import logging
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, TYPE_CHECKING

from mechus.json_utils import safe_parse_json
from mechus.meeting_chapters import format_chapters_for_prompt, load_chapters
from mechus.meeting_store import StoredChunk
from mechus.notes_paths import resolve_meeting_store_dir

if TYPE_CHECKING:
    from mechus.context_integrity_engine import FinalReport

log = logging.getLogger(__name__)

_SALIENT_TAGS = frozenset({"decision", "action_item", "open_question"})

# Owner strings that really mean "nobody specific was assigned".
_UNASSIGNED_OWNERS = frozenset({
    "", "unassigned", "someone", "somebody", "anyone", "none", "n/a", "na",
    "tbd", "to be determined", "nobody", "no one", "null", "-", "?",
})
# Deadline strings that really mean "no deadline".
_EMPTY_DEADLINES = frozenset({
    "", "none", "n/a", "na", "tbd", "to be determined", "null", "no deadline",
    "not specified", "unspecified", "-", "?", "asap?",
})
# Tasks this short or generic are noise, not real commitments.
_JUNK_TASK_TOKENS = frozenset({
    "", "do it", "do this", "follow up", "followup", "tbd", "n/a", "stuff",
    "things", "it", "this", "that", "—", "-",
})
# Pseudo-tasks from assistant Q&A / room troubleshooting — not team deliverables.
_JUNK_TASK_PHRASES = re.compile(
    r"\b("
    r"ask\s+mechus|tell\s+mechus|hey\s+mechus|clarify\s+audio|audio\s+quality|"
    r"voice\s+change|sound\s+lazy|repeat\s+that|can\s+you\s+hear"
    r")\b",
    re.I,
)
_JUNK_OPEN_QUESTION_RE = re.compile(
    r"^(?:hey\s+)?(?:mechus|koo|cool|coop|kuv|computer)\b|"
    r"\b(?:fuck|shit|damn)\b|"
    r"^(?:time\s+right|why\s+sound|hey\s+cool)\b",
    re.I,
)


def _coerce_scalar_text(val: Any, *, keys: tuple[str, ...] = ()) -> str:
    """Extract a display string from LLM/state values (handles nested dicts)."""
    if val is None:
        return ""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, (int, float, bool)):
        return str(val).strip()
    if isinstance(val, dict):
        for k in keys + ("value", "text", "task", "description", "label", "name", "question", "decision"):
            if k in val:
                s = _coerce_scalar_text(val[k], keys=keys)
                if s:
                    return s
        return ""
    if isinstance(val, list):
        parts = [_coerce_scalar_text(x, keys=keys) for x in val]
        return " ".join(p for p in parts if p).strip()
    return ""


def _is_junk_task(task: str) -> bool:
    t = (task or "").strip()
    if not t or len(t) < 4:
        return True
    if _task_key(t) in _JUNK_TASK_TOKENS:
        return True
    if _JUNK_TASK_PHRASES.search(t):
        return True
    if "[object object]" in t.lower():
        return True
    return False


def _is_junk_open_question(q: str) -> bool:
    t = re.sub(r"\s+", " ", (q or "").strip())
    # A short string ending in a question mark can still be a meaningful unresolved
    # item ("Budget?", "When?", etc.).  The old length-only guard discarded those
    # when merging the deterministic final report into a fallback summary.
    if not t or len(t) < 6 or (len(t) < 12 and "?" not in t):
        return True
    if "[object object]" in t.lower():
        return True
    if _JUNK_OPEN_QUESTION_RE.search(t):
        return True
    # Topic-key slugs from open_issues (e.g. "time right", "hey cool") — not real questions.
    if "?" not in t and len(t.split()) <= 3:
        return True
    return False


def _norm_owner(owner: Any) -> str:
    """Normalize an action-item owner. Empty/placeholder -> 'Unassigned'."""
    o = str(owner or "").strip().strip(".,;:")
    if o.lower() in _UNASSIGNED_OWNERS:
        return "Unassigned"
    # Collapse internal whitespace; keep author casing (names, "the team", etc.)
    o = re.sub(r"\s+", " ", o)
    return o


def _norm_deadline(deadline: Any) -> str | None:
    """Normalize a deadline. Placeholder/empty -> None."""
    d = str(deadline or "").strip().strip(".,;:")
    if d.lower() in _EMPTY_DEADLINES:
        return None
    return re.sub(r"\s+", " ", d)


def _norm_task(task: Any) -> str:
    """Normalize a task phrase: drop leading modal/'to', trim trailing punctuation."""
    s = _coerce_scalar_text(task, keys=("task", "description", "action", "text"))
    s = re.sub(r"^(?:will|to|should|must|need(?:s)?\s+to|is\s+going\s+to|"
               r"has\s+to|going\s+to|gonna)\s+", "", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip(" .;,")
    return s


def _task_key(task: str) -> str:
    """Fuzzy key for dedup: lowercase alnum tokens only."""
    return re.sub(r"[^a-z0-9]+", " ", task.lower()).strip()


def _normalize_action_items(items: Any) -> list[dict[str, Any]]:
    """Clean, validate, and de-duplicate action items from LLM or state output.

    - Accepts list of dicts OR plain strings (robust to model variance).
    - Drops junk (empty/too-short/generic tasks).
    - Normalizes owner ('Unassigned' default) and deadline (None default).
    - De-dupes on (owner, fuzzy task), keeping the richer / higher-confidence entry.
    - Sorts assigned items before unassigned, then by confidence desc.
    """
    if not isinstance(items, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for it in items:
        if isinstance(it, str):
            it = {"task": it}
        if not isinstance(it, dict):
            continue
        task = _norm_task(it.get("task") or it.get("action") or it.get("description"))
        if not task or _is_junk_task(task):
            continue
        owner = _norm_owner(it.get("owner") or it.get("assignee") or it.get("who"))
        # A task that is just the owner's name again is noise.
        if _task_key(task) and _task_key(task) == _task_key(owner):
            continue
        deadline = _norm_deadline(
            it.get("deadline") or it.get("due") or it.get("due_date") or it.get("when")
        )
        try:
            conf = float(it.get("confidence"))
            conf = max(0.0, min(1.0, conf))
        except (TypeError, ValueError):
            conf = 0.8
        source = str(it.get("source") or "transcript").strip() or "transcript"
        cleaned.append({
            "owner": owner,
            "assignee": owner,
            "task": task,
            "deadline": deadline,
            "due_date": deadline,
            "source": source,
            "confidence": round(conf, 2),
        })

    # De-dupe on (owner, fuzzy task). Keep the entry with more info / higher conf.
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for item in cleaned:
        key = (item["owner"].lower(), _task_key(item["task"]))
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = item
            order.append(key)
            continue
        # Prefer the one with a deadline, else higher confidence, else longer task.
        better = item
        if existing.get("deadline") and not item.get("deadline"):
            better = existing
        elif item.get("deadline") and not existing.get("deadline"):
            better = item
        elif existing.get("confidence", 0) >= item.get("confidence", 0):
            better = existing
        by_key[key] = better
    deduped = [by_key[k] for k in order]

    def _sort_key(a: dict[str, Any]) -> tuple:
        unassigned = a["owner"] == "Unassigned"
        return (unassigned, -a.get("confidence", 0.0))

    return sorted(deduped, key=_sort_key)


def _normalize_decisions(items: Any) -> list[dict[str, Any]]:
    """Clean and de-dupe decisions. Accepts dicts or strings."""
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    idx = 0
    for it in items:
        if isinstance(it, str):
            value = it
            rationale = None
            source = "transcript"
        elif isinstance(it, dict):
            value = _coerce_scalar_text(
                it.get("value") or it.get("decision") or it.get("text"),
                keys=("value", "decision", "text"),
            )
            rationale = it.get("rationale")
            source = str(it.get("source") or "transcript").strip() or "transcript"
        else:
            continue
        value = re.sub(r"\s+", " ", value).strip(" .;,")
        if not value or len(value) < 4 or "[object object]" in value.lower():
            continue
        key = _task_key(value)[:60]
        if key in seen:
            continue
        seen.add(key)
        entry: dict[str, Any] = {"id": (it.get("id") if isinstance(it, dict) else None) or f"d{idx}", "value": value, "source": source}
        rationale = (str(rationale).strip() if rationale else "")
        if rationale and rationale.lower() not in ("none", "n/a", "null"):
            entry["rationale"] = rationale
        out.append(entry)
        idx += 1
    return out


def _normalize_questions(items: Any) -> list[str]:
    """Clean and de-dupe open questions; ensure they read as questions."""
    if not isinstance(items, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for it in items:
        q = it.get("question") if isinstance(it, dict) else it
        q = _coerce_scalar_text(q, keys=("question", "text", "value"))
        q = re.sub(r"\s+", " ", q)
        if not q or _is_junk_open_question(q):
            continue
        key = _task_key(q)[:60]
        if key in seen:
            continue
        seen.add(key)
        out.append(q)
    return out


def _dedupe_strings(items: Any, *, min_len: int = 2) -> list[str]:
    """Clean + de-dupe a list of short strings (topics, risks)."""
    if not isinstance(items, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for it in items:
        s = re.sub(r"\s+", " ", str(it or "").strip()).strip(" .;,")
        if not s or len(s) < min_len:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _merge_action_item_sources(*sources: Any) -> list[dict[str, Any]]:
    """Union action items from LLM, canonical state, chapter digests, and FinalReport."""
    combined: list[dict[str, Any]] = []
    for src in sources:
        if not src:
            continue
        if isinstance(src, list) and src and isinstance(src[0], tuple):
            # FinalReport.action_items_table: (owner, task, deadline, ts)
            combined.extend(
                _normalize_action_items([
                    {"owner": o, "task": t, "deadline": d, "source": "transcript"}
                    for o, t, d, _ts in src
                ])
            )
        else:
            combined.extend(_normalize_action_items(src))
    if not combined:
        return []
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for item in combined:
        key = (item["owner"].lower(), _task_key(item["task"]))
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = item
            order.append(key)
            continue
        # Prefer richer row: deadline wins, then higher confidence, then longer task.
        better = item
        if existing.get("deadline") and not item.get("deadline"):
            better = existing
        elif item.get("deadline") and not existing.get("deadline"):
            better = item
        elif existing.get("confidence", 0) > item.get("confidence", 0):
            better = existing
        elif len(existing.get("task") or "") > len(item.get("task") or ""):
            better = existing
        by_key[key] = better
    merged = [by_key[k] for k in order]

    def _sort_key(a: dict[str, Any]) -> tuple:
        unassigned = a["owner"] == "Unassigned"
        has_deadline = 0 if a.get("deadline") else 1
        return (unassigned, has_deadline, -a.get("confidence", 0.0))

    return sorted(merged, key=_sort_key)


def _is_assistant_speaker(label: str) -> bool:
    s = (label or "").strip().lower()
    return s in ("mechus", "assistant", "bot", "ai") or s.startswith("mechus ")


def _collect_participant_names(chunks: list[StoredChunk]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for c in chunks:
        spk = (c.speaker_display or c.speaker_id or "").strip()
        if not spk or _is_assistant_speaker(spk):
            continue
        key = spk.lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(spk)
    return names


def _format_action_items_for_prompt(items: list[dict[str, Any]]) -> str:
    if not items:
        return "(none captured in structured state)"
    lines: list[str] = []
    for a in items[:40]:
        owner = a.get("owner") or "Unassigned"
        task = a.get("task") or ""
        deadline = a.get("deadline")
        line = f"- {owner}: {task}"
        if deadline:
            line += f" (due {deadline})"
        lines.append(line)
    return "\n".join(lines)


def extract_action_items_with_llm(
    transcript_text: str,
    *,
    known_items: list[dict[str, Any]] | None = None,
    participant_names: list[str] | None = None,
    api_key: str,
    model: str = "gpt-4o-mini",
    timeout_seconds: int = 50,
    max_transcript_chars: int = 22000,
) -> list[dict[str, Any]]:
    """Focused second pass: assignments with owner + due date (main deliverable for teams)."""
    if not api_key or not (transcript_text or "").strip():
        return []
    ttext = (transcript_text or "").strip()
    if len(ttext) > max_transcript_chars:
        ttext = ttext[: max_transcript_chars - 40].rsplit("\n", 1)[0] + "\n(…truncated)"
    roster = ", ".join(participant_names or []) or "(names from transcript)"
    known_block = _format_action_items_for_prompt(known_items or [])
    system = (
        "You extract HUMAN action items from a meeting transcript.\n"
        "Return ONLY JSON: {\"action_items\": [{\"owner\": str, \"task\": str, "
        "\"deadline\": str|null, \"confidence\": number, \"source\": \"transcript\"}]}\n"
        "\n"
        "Each action item is a concrete assignment:\n"
        "  owner — person responsible (use roster names when possible; 'Unassigned' if unclear)\n"
        "  task — verb-first commitment (send the pricing deck, book the venue, update the API spec)\n"
        "  deadline — exact phrase or date from the meeting (Friday, July 1, end of Q3), or null\n"
        "\n"
        "INCLUDE patterns like:\n"
        "  'Alex will finalize the contract by July 1'\n"
        "  'Can you take the deck?' → owner is whoever was asked\n"
        "  'That's on Priya' / 'Marcus is on staging'\n"
        "  'I'll send the recap by EOD' → owner = speaker name from transcript label\n"
        "  'We need Sam to schedule interviews this week'\n"
        "\n"
        "EXCLUDE:\n"
        "  Lines tagged [assistant] or spoken by Mechus (AI assistant Q&A, not team work)\n"
        "  Questions TO the assistant ('Hey Mechus, what's the weather?')\n"
        "  Vague chat with no assignee ('we should think about that')\n"
        "  Scheduling the next meeting / goodbyes\n"
        "\n"
        "Never invent owners or deadlines. Split 'do X and Y' into two items. "
        "If structured state already lists an item, include it (refine wording if transcript is clearer)."
    )
    user_msg = (
        f"## Participant roster\n{roster}\n\n"
        f"## Already captured in structured state (merge + refine; do not drop)\n{known_block}\n\n"
        f"## Transcript evidence\n{ttext}\n\n"
        "Return the action_items array."
    )
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
        "max_tokens": 1200,
        "temperature": 0.15,
        **({"response_format": {"type": "json_object"}} if _mechus_json_ok() else {}),
    }
    req = urllib.request.Request(
        _mechus_chat_url(),
        data=json.dumps(body).encode("utf-8"),
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
        with urllib.request.urlopen(req, timeout=timeout_seconds, context=ctx) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log.warning("Action-item extraction LLM failed: %s", exc)
        return []
    choices = payload.get("choices") or []
    if not choices:
        return []
    content = (choices[0].get("message") or {}).get("content") or ""
    parsed = safe_parse_json(content.strip())
    if parsed.get("_parse_failed"):
        return []
    return _normalize_action_items(parsed.get("action_items"))


def _format_stored_line(c: StoredChunk) -> str:
    mins = int(c.ts // 60)
    secs = int(c.ts % 60)
    tag_note = f" [{', '.join(c.tags)}]" if c.tags else ""
    spk = c.speaker_display or c.speaker_id
    if _is_assistant_speaker(spk):
        spk = f"{spk} [assistant]"
    return f"[{mins:02d}:{secs:02d}] {spk}{tag_note}: {c.text.strip()}"


def _even_time_sample(full_lines: list[str], budget: int) -> str:
    """Sample transcript lines EVENLY across the whole meeting timeline within a
    char budget. Unlike head/middle/tail windowing, this gives uniform coverage
    from open to close, so the summary reflects the entire conversation instead
    of over-weighting the most recent minutes. Skipped spans are marked with '…'.
    """
    if budget <= 0:
        return ""
    joined = "\n".join(full_lines)
    if len(joined) <= budget:
        return joined
    n = len(full_lines)
    if n == 0:
        return ""
    if n == 1:
        return full_lines[0][:budget]

    # Reserve room for the final line so the meeting's close is always represented.
    last_line = full_lines[-1]
    last_reserve = len(last_line) + 1 + 2  # line + newline + gap marker
    body_budget = max(0, budget - last_reserve)

    middle = full_lines[:-1]
    joined_mid = "\n".join(middle)
    ratio = (body_budget / len(joined_mid)) if joined_mid else 0.0
    kept: list[str] = []
    used = 0
    acc = 0.0
    skipped_since_last = False
    for i, line in enumerate(middle):
        force = (i == 0)
        acc += ratio
        take = force or acc >= 1.0
        if take:
            acc = max(0.0, acc - 1.0)
            gap_cost = 2 if (skipped_since_last and kept) else 0
            if used + len(line) + 1 + gap_cost > body_budget:
                skipped_since_last = True
                continue
            if gap_cost:
                kept.append("…")
                used += gap_cost
            kept.append(line)
            used += len(line) + 1
            skipped_since_last = False
        else:
            skipped_since_last = True

    # Always append the final line (with a gap marker if we skipped before it).
    if skipped_since_last and kept:
        kept.append("…")
    kept.append(last_line)
    return "\n".join(kept)


def build_summary_evidence_text(
    chunks: list[StoredChunk],
    *,
    max_chars: int = 28000,
    meeting_id: str | None = None,
    chapters_base_dir: str | None = None,
) -> str:
    """
    Bounded transcript text for end-of-meeting summarization: optional rolling chapter
    summaries (map), then salient tagged chunks, then an EVEN time-stratified sample of
    the full transcript (uniform coverage open→close, not end-weighted).
    """
    ch_header = ""
    if meeting_id:
        base = chapters_base_dir or os.environ.get("MECHUS_MEETING_STORE_DIR", "mechus_meetings")
        ch = load_chapters(base, meeting_id)
        ch_budget = min(12000, max(1500, max_chars // 3))
        ch_text = format_chapters_for_prompt(ch, max_chars=ch_budget)
        if ch_text.strip():
            ch_header = "### Rolling chapter summaries (map)\n" + ch_text + "\n\n"

    inner_max = max_chars - len(ch_header)
    if inner_max < 800:
        ch_header = ""
        inner_max = max_chars

    valid = [c for c in chunks if (c.text or "").strip()]
    if not valid:
        return ch_header.strip() if ch_header else "(No transcript captured.)"
    participants = _collect_participant_names(valid)
    if participants:
        roster = "### Participants (human)\n" + ", ".join(participants) + "\n\n"
        ch_header = ch_header + roster
        inner_max = max_chars - len(ch_header)
    valid.sort(key=lambda c: c.ts)
    full_lines = [_format_stored_line(c) for c in valid]
    full_text = "\n".join(full_lines)
    if len(full_text) <= inner_max:
        body = full_text
        return (ch_header + body).strip() if ch_header else body

    salient = [c for c in valid if _SALIENT_TAGS.intersection(c.tags)]
    seen: set[tuple[Any, ...]] = set()
    salient_unique: list[StoredChunk] = []
    for c in sorted(salient, key=lambda x: x.ts):
        key = (c.chunk_id, round(c.ts, 3), c.text[:120])
        if key in seen:
            continue
        seen.add(key)
        salient_unique.append(c)
    salient_block = "\n".join(_format_stored_line(c) for c in salient_unique)
    header_salient = "### Salient tagged chunks (decisions, actions, open questions)\n"
    header_even = "\n### Full meeting sample (even coverage, open → close)\n"
    overhead = len(header_salient) + len(header_even) + 120

    if not salient_unique:
        remainder = inner_max - len(header_even) - 60
        body = _even_time_sample(full_lines, remainder)
        out = header_even.strip("\n") + "\n" + body
        out = (ch_header + out) if ch_header else out
        if len(out) > max_chars:
            out = out[: max_chars - 20].rsplit("\n", 1)[0] + "\n(…truncated)"
        return out or "(No transcript captured.)"

    sal_budget = min(len(salient_block), max(inner_max // 2, inner_max - 8000))
    salient_trim = salient_block[:sal_budget] if len(salient_block) > sal_budget else salient_block
    remainder = inner_max - len(salient_trim) - overhead
    if remainder < 400:
        out = header_salient + salient_trim
        out = (ch_header + out) if ch_header else out
        return out[:max_chars] if len(out) > max_chars else out

    body = _even_time_sample(full_lines, remainder)
    pieces: list[str] = [header_salient + salient_trim, header_even + body]
    out = "\n".join(pieces)
    out = (ch_header + out) if ch_header else out
    if len(out) > max_chars:
        out = out[: max_chars - 20].rsplit("\n", 1)[0] + "\n(…truncated)"
    return out


def merge_final_report_into_summary(summary: dict[str, Any], report: "FinalReport | None") -> dict[str, Any]:
    """Merge deterministic FinalReport fields into the dashboard summary JSON when LLM output is thin or missing."""
    if report is None:
        return summary
    out = dict(summary)
    src = out.get("source")
    if src in ("fallback", "state_only") or not (str(out.get("executive_summary") or "").strip()):
        out["executive_summary"] = report.executive_summary
    if not out.get("decisions"):
        out["decisions"] = _normalize_decisions([
            {"id": f"r{i}", "value": d, "source": "transcript"}
            for i, d in enumerate(report.final_decisions)
        ])
    oq = _normalize_questions(
        list(out.get("open_questions") or []) + list(report.unresolved_questions or [])
    )
    out["open_questions"] = oq
    report_items = report.action_items_table if report.action_items_table else []
    out["action_items"] = _merge_action_item_sources(
        out.get("action_items"),
        report_items,
    )
    out["final_report_extras"] = {
        "contradictions": report.contradictions_identified[:25],
        "risks": report.risks_identified[:25],
        "confidence_score": report.confidence_score,
        "confidence_reasoning": report.confidence_reasoning,
    }
    return out


def build_transcript_for_llm(chunks: list[tuple[float, str, str]], max_chars: int = 50000) -> str:
    """
    Format stored chunks as a single transcript string for the LLM.
    Each line: [timestamp] Speaker: text
    """
    if not chunks:
        return "(No transcript captured.)"
    lines = []
    for ts, speaker_id, text in chunks:
        if not (text or "").strip():
            continue
        # Optional: format timestamp as MM:SS for readability
        mins = int(ts // 60)
        secs = int(ts % 60)
        line = f"[{mins:02d}:{secs:02d}] {speaker_id}: {text.strip()}"
        lines.append(line)
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[-max_chars:].strip()
        out = "(Transcript truncated from start.)\n\n" + out
    return out or "(No transcript captured.)"


_TRANSCRIPT_DUMP_MARKERS = (
    "summary from transcript",
    "(no transcript captured.)",
    "(transcript truncated",
    "### participants",
    "### salient",
    "### time-window",
    "### chapter",
)
_TIMESTAMP_LINE_RE = re.compile(r"^\s*\[\d{1,2}:\d{2}\]", re.MULTILINE)
_SPEAKER_LINE_RE = re.compile(
    r"^\s*(?:\[\d{1,2}:\d{2}\]\s*)?[\w][\w .'-]{0,40}:\s+\S",
    re.MULTILINE,
)


def is_executive_summary_raw_transcript_dump(text: str | None) -> bool:
    """True when ``executive_summary`` looks like a pasted transcript, not a real summary."""
    t = (text or "").strip()
    if not t:
        return False
    low = t.lower()
    if any(m in low for m in _TRANSCRIPT_DUMP_MARKERS):
        return True
    if len(_TIMESTAMP_LINE_RE.findall(t)) >= 2:
        return True
    if len(_SPEAKER_LINE_RE.findall(t)) >= 3:
        return True
    if len(t) > 1800 and t.count("\n") >= 8:
        return True
    if len(t) > 1200 and len(re.findall(r"\[\d{1,2}:\d{2}\]", t)) >= 2:
        return True
    return False


def summarize_transcript_brief_with_llm(
    transcript_text: str,
    *,
    api_key: str,
    model: str = "gpt-4o-mini",
    timeout_seconds: int = 45,
    max_transcript_chars: int = 16000,
) -> str:
    """Focused prose summary when the main JSON summary path fails or returns a transcript dump."""
    if not api_key or not (transcript_text or "").strip():
        return ""
    ttext = (transcript_text or "").strip()
    if ttext == "(No transcript captured.)":
        return ""
    if len(ttext) > max_transcript_chars:
        ttext = ttext[: max_transcript_chars - 40].rsplit("\n", 1)[0] + "\n(…truncated)"
    system = (
        "Write a concise executive summary of this meeting in 4–6 clear sentences.\n"
        "Cover what was discussed, key decisions, next steps, and open questions.\n"
        "Use natural prose — NOT a transcript, NOT speaker-by-speaker quotes, NOT timestamps.\n"
        "Ignore Mechus assistant Q&A unless it was central to team decisions."
    )
    user_msg = f"## Meeting transcript evidence\n{ttext}\n\nWrite the executive summary."
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
        "max_tokens": 450,
        "temperature": 0.25,
    }
    req = urllib.request.Request(
        _mechus_chat_url(),
        data=json.dumps(body).encode("utf-8"),
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
        with urllib.request.urlopen(req, timeout=timeout_seconds, context=ctx) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log.warning("Brief transcript summary LLM failed: %s", exc)
        return ""
    choices = payload.get("choices") or []
    if not choices:
        return ""
    content = (choices[0].get("message") or {}).get("content") or ""
    out = re.sub(r"\s+", " ", content.strip())
    if not out or is_executive_summary_raw_transcript_dump(out):
        return ""
    return out


def _coerce_executive_summary(
    candidate: str | None,
    *,
    fallback: str = "",
    transcript_text: str | None = None,
    api_key: str | None = None,
    model: str = "gpt-4o-mini",
    timeout_seconds: int = 45,
) -> str:
    """Return a real executive summary — never a raw transcript dump."""
    c = (candidate or "").strip()
    if c and not is_executive_summary_raw_transcript_dump(c):
        return c
    fb = (fallback or "").strip()
    if fb and not is_executive_summary_raw_transcript_dump(fb):
        return fb
    if api_key and (transcript_text or "").strip():
        brief = summarize_transcript_brief_with_llm(
            transcript_text or "",
            api_key=api_key,
            model=model,
            timeout_seconds=timeout_seconds,
        )
        if brief:
            return brief
    if fb:
        return fb
    return "Meeting ended — a structured summary could not be generated from this session."


def _meeting_store_base(base_dir: str | None = None) -> str:
    if base_dir:
        return os.path.abspath(os.path.expanduser(base_dir))
    return resolve_meeting_store_dir()


def _action_items_from_transcript_fallback(
    transcript_text: str,
    participant_names: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Regex/heuristic pass when structured state + LLM missed spoken commitments."""
    if not (transcript_text or "").strip():
        return []
    try:
        from mechus.context_integrity_engine import _extract_action_items
    except Exception:
        return []
    known = {str(n).strip() for n in (participant_names or []) if str(n).strip()}
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for raw_line in (transcript_text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("###"):
            continue
        speaker = "unknown"
        text = line
        if ":" in line[:80]:
            head, _, tail = line.partition(":")
            if head.strip() and tail.strip() and len(head.split()) <= 4:
                if head.strip().startswith("[") or re.match(r"^\d{1,2}:\d{2}", head.strip()):
                    text = line
                else:
                    speaker = head.strip()
                    text = tail.strip()
        if text.startswith("[") and "]" in text[:12]:
            continue
        for ai in _extract_action_items(text, speaker, 0.0, 0, known):
            key = (ai.owner.strip().lower(), ai.task.strip().lower())
            if key in seen or not ai.task:
                continue
            seen.add(key)
            out.append(
                {
                    "owner": ai.owner or "Unassigned",
                    "task": ai.task,
                    "deadline": ai.deadline,
                    "source": "transcript_heuristic",
                }
            )
    return out[:20]


def _fallback_executive_summary(
    transcript_text: str | None,
    *,
    api_key: str | None = None,
    model: str = "gpt-4o-mini",
    timeout_seconds: int = 45,
) -> str:
    """Last-resort executive summary text when structured state is empty."""
    return _coerce_executive_summary(
        "",
        fallback="",
        transcript_text=transcript_text,
        api_key=api_key,
        model=model,
        timeout_seconds=timeout_seconds,
    )


def minimal_final_summary_from_transcript_and_state(transcript_text: str, state: dict) -> dict[str, Any]:
    """
    Last-resort summary when generate_final_summary or merge fails entirely.
    Same shape as generate_final_summary fallback output.
    """
    decisions = state.get("decisions") or []
    action_items = state.get("action_items") or []
    open_issues = state.get("open_issues") or []
    open_questions = [
        q for q in (
            _coerce_scalar_text(
                i.get("display_summary") or i.get("topic_key") or "",
                keys=("display_summary", "topic_key", "question"),
            )
            for i in open_issues
            if (i.get("status") or "") == "open"
        )
        if q and not _is_junk_open_question(q)
    ]
    return _fallback_summary_from_state(
        decisions, action_items, open_questions, transcript_text=transcript_text
    )


def build_live_partial_summary(transcript_text: str, state: dict) -> dict[str, Any]:
    """Rolling recap for live dashboard (no LLM — context engine + spoken recap only)."""
    fs = generate_final_summary(transcript_text, state, api_key=None, mode="state")
    fs["live"] = True
    return fs


def actionable_items_from_summary(fs: dict[str, Any] | None) -> list[str]:
    """Flatten action_items from a final/live summary dict for transcript API payloads."""
    if not fs:
        return []
    actionable: list[str] = []
    for item in fs.get("action_items") or []:
        if isinstance(item, str):
            t = item.strip()
            if t:
                actionable.append(t)
        elif isinstance(item, dict):
            task = str(item.get("task") or item.get("text") or "").strip()
            if not task:
                continue
            owner = str(item.get("owner") or item.get("assignee") or "").strip()
            deadline = str(item.get("deadline") or item.get("due_date") or "").strip()
            line = f"{owner}: {task}" if owner else task
            if deadline:
                line += f" (by {deadline})"
            actionable.append(line)
    return actionable


def live_recap_to_transcript_payload(fs: dict[str, Any] | None) -> dict[str, Any]:
    """Map a live partial summary into GET /api/transcript recap fields."""
    if not fs:
        return {"summary": "", "final_summary": None, "actionable_items": [], "live_recap": False}
    exec_sum = str(fs.get("executive_summary") or "").strip()
    if exec_sum and is_executive_summary_raw_transcript_dump(exec_sum):
        exec_sum = ""
    actionable = actionable_items_from_summary(fs)
    decisions = fs.get("decisions") or []
    open_questions = fs.get("open_questions") or []
    has_content = bool(
        exec_sum
        or actionable
        or decisions
        or open_questions
    )
    return {
        "summary": exec_sum,
        "final_summary": fs if has_content else None,
        "actionable_items": actionable,
        "live_recap": has_content,
    }


def generate_final_summary(
    transcript_text: str,
    state: dict,
    *,
    api_key: str | None = None,
    model: str = "gpt-4o-mini",
    timeout_seconds: int = 120,
    mode: str = "full",
    max_transcript_chars: int = 28000,
    fast_max_tokens: int = 1400,
    full_max_tokens: int = 2400,
    prior_sessions_context: str | None = None,
    participant_names: list[str] | None = None,
) -> dict[str, Any]:
    """
    Send transcript + current state to the LLM and return structured summary
    for the dashboard: executive_summary, action_items, decisions, open_questions.

    prior_sessions_context: Optional text block describing earlier sessions of the
    same recurring meeting link (from _format_prior_meetings_for_prompt). When
    provided it is injected as an extra section so the LLM can note continuity,
    carry-forward action items, and resolved vs still-open threads.

    mode:
      - state: no LLM; state-only JSON (same shape as no-api-key path).
      - fast: lower max_tokens for speed; transcript is already bounded by build_summary_evidence_text.
      - full: large transcript budget (evidence text is bounded by max_transcript_chars upstream).

    If api_key is None or empty, returns a non-LLM fallback built from state only.
    """
    mode = (mode or "full").strip().lower()
    if mode not in ("state", "fast", "full"):
        mode = "full"
    decisions = state.get("decisions") or []
    action_items = state.get("action_items") or []
    open_issues = state.get("open_issues") or []
    open_questions = [
        q for q in (
            _coerce_scalar_text(
                i.get("display_summary") or i.get("topic_key") or "",
                keys=("display_summary", "topic_key", "question"),
            )
            for i in open_issues
            if (i.get("status") or "") == "open"
        )
        if q and not _is_junk_open_question(q)
    ]

    if mode == "state" or not api_key or not api_key.strip():
        # No LLM: build a simple summary from state only
        exec_summary_parts = []
        if decisions:
            exec_summary_parts.append(f"Decisions made: {len(decisions)}.")
        if action_items:
            exec_summary_parts.append(f"Action items: {len(action_items)}.")
        if open_questions:
            exec_summary_parts.append(f"Open questions: {len(open_questions)}.")
        if exec_summary_parts:
            executive_summary = " ".join(exec_summary_parts)
        else:
            executive_summary = _fallback_executive_summary(
                (transcript_text or "").strip(),
                api_key=None,
            )
        spoken_recap = (state.get("spoken_executive_recap") or "").strip()
        if spoken_recap and not is_executive_summary_raw_transcript_dump(spoken_recap):
            executive_summary = spoken_recap
        norm_actions = _normalize_action_items([
            {**a, "source": a.get("source") or "state"} for a in action_items
        ])
        if not norm_actions:
            norm_actions = _merge_action_item_sources(
                norm_actions,
                _action_items_from_transcript_fallback(
                    transcript_text,
                    participant_names=participant_names or state.get("participant_names"),
                ),
            )
        return {
            "title": _infer_title_from_topics(None, executive_summary),
            "executive_summary": executive_summary,
            "action_items": norm_actions,
            "decisions": _normalize_decisions(decisions),
            "open_questions": _normalize_questions(open_questions),
            "source": "state_only",
        }

    ttext = (transcript_text or "").strip()
    if len(ttext) > max_transcript_chars:
        ttext = ttext[: max_transcript_chars - 40].rsplit("\n", 1)[0] + "\n(…truncated)"
    eff_timeout = timeout_seconds
    max_tokens = fast_max_tokens if mode == "fast" else full_max_tokens
    if mode == "fast":
        eff_timeout = min(eff_timeout, 75)

    system = (
        "You sat in on this meeting as a teammate. Write the summary the team will actually read.\n"
        "You receive: (1) authoritative structured state, (2) optional rolling chapter summaries (the\n"
        "map step over long meetings), (3) salient tagged lines, (4) time-window samples.\n"
        "If prior session context is provided (section PRIOR_SESSION_CONTEXT), use it ONLY to:\n"
        "  - note carry-forward open threads or action items from last time that were addressed today\n"
        "  - identify action items from prior sessions that appear still unresolved\n"
        "  - add a brief 'continuity' sentence if meaningful progress was made vs last time\n"
        "  Do NOT let prior sessions dominate the executive_summary — today's meeting is primary.\n"
        "\n"
        "Map-reduce contract (this is how to handle ANY length, including 2+ hour meetings):\n"
        "- Chapter summaries already compress earlier portions. Treat them as authoritative for facts\n"
        "  outside the time windows you can see, and stitch them with the live windows in chronological order.\n"
        "- The transcript sample is spread EVENLY across the whole meeting (open → close). Cover the WHOLE\n"
        "  arc — opening topics matter as much as closing ones. Do NOT over-weight the final minutes.\n"
        "- Never claim the meeting only covered what's in the time windows when chapters mention more topics —\n"
        "  use the chapters to fill in earlier ground.\n"
        "- IGNORE end-of-call wind-down: goodbyes, 'alright that's it', scheduling the next call, small talk,\n"
        "  and any Q&A directed at the assistant. These are NOT meeting content — don't let them shape the summary.\n"
        "- For FACTUAL conflicts only (a number/date/decision that was revised), the later statement wins.\n"
        "  This is about correctness, NOT about emphasis — never let recency inflate how much a topic matters.\n"
        "- Weight a topic by how much it was actually discussed across the whole meeting, not by when it occurred.\n"
        "\n"
        "Voice and accuracy:\n"
        "- Write like a sharp, honest colleague — natural sentences, no corporate filler, no jargon.\n"
        "- Use real names when you have them. 'Alex locked in July 25' beats 'a decision was made'.\n"
        "- Be specific. Numbers, dates, names, dollar amounts — keep them exact.\n"
        "- If something is uncertain, say 'we didn't fully nail down X' rather than inventing.\n"
        "- Never editorialize how people spoke (no 'heated', 'chaotic', 'bickering'). Just WHAT and WHY.\n"
        "- Never repeat the same phrase twice. Vary structure. Don't copy transcript lines verbatim.\n"
        "- Filter noise: ignore TV/radio ads, sirens, unrelated media, and clearly garbled background lines. "
        "Summarize what participants actually discussed, decided, and committed to — not ambient playback.\n"
        "\n"
        "JSON shape (output ONLY this object, no markdown, no code fence):\n"
        "  title (string): 3–7 word meeting title a human would actually use to find this meeting later.\n"
        "      Name the MAIN topic(s) — NOT generic. Good: 'Q3 Launch Planning', 'Backend API Review',\n"
        "      'Hiring + Roadmap Prioritization', 'Weekly Standup — Jun 10'. Bad: 'Meeting', 'Team Sync'.\n"
        "      Always include a title — never return empty string.\n"
        "  executive_summary (string): 4–6 clear sentences for normal meetings; up to 10 for 60+ min meetings.\n"
        "      Most important thing first. Cover EVERY major topic discussed, what got decided, what's next,\n"
        "      and any open risks or blockers. Touch every topic — don't skip one just because state is empty.\n"
        "      Write it like a short overview a teammate would actually read — specific, human, no filler.\n"
        "      If prior session context shows a carry-forward thread that was resolved today, mention it in one sentence.\n"
        "  action_items (array of objects): EVERY commitment that was made — explicit OR implied.\n"
        "      This is the PRIMARY deliverable for the team. Each item must name WHO does WHAT by WHEN.\n"
        "      Include items where someone was directly assigned, asked, or volunteered to do something.\n"
        "      This includes: 'X will do Y', 'X is assigned to Y', 'X was asked to Y', 'that's on X',\n"
        "      'we need X to Y', 'X has been assigned Y', 'X is taking care of Y', passive voice assignments.\n"
        "      Each: {\"owner\": \"<person name or 'Unassigned'>\", \"assignee\": \"<same as owner>\",\n"
        "             \"task\": \"<concrete verb-phrase — self-contained, no it/that>\",\n"
        "             \"deadline\": \"<ISO date or natural phrase or null>\",\n"
        "             \"due_date\": \"<same as deadline or null>\",\n"
        "             \"source\": \"state\" | \"transcript\" | \"chapter\" | \"prior_session\",\n"
        "             \"confidence\": <0.0–1.0>}.\n"
        "      Example: {\"owner\": \"Priya\", \"assignee\": \"Priya\", \"task\": \"finalize the API contract\",\n"
        "                \"deadline\": \"July 1\", \"due_date\": \"July 1\", \"source\": \"transcript\", \"confidence\": 0.92}\n"
        "      If prior_sessions_context shows items that appear still unresolved, include them with "
        "source='prior_session' and confidence ≤ 0.6.\n"
        "      Merge with structured state; if state already has it, copy; if transcript adds nuance, refine.\n"
        "      Never invent deadlines. confidence < 0.5 means 'mentioned but unclear'.\n"
        "      Do NOT create action items from closing small talk, 'let's wrap up', scheduling the next meeting,\n"
        "      or anything said to the assistant — only real work commitments from the discussion count.\n"
        "      Never turn Mechus Q&A into action items (e.g. 'ask Mechus about X', 'clarify audio issues').\n"
        "      Each task must be SELF-CONTAINED and start with a verb — name the actual thing, never 'it'/'that'\n"
        "      (write 'send the pricing deck to Sarah', not 'send it over'). Owner is a person's name when known,\n"
        "      never a pronoun. One commitment per item — split 'do X and Y' into two. No duplicates.\n"
        "  decisions (array of objects): every decision the group landed on.\n"
        "      Each: {\"id\": \"<short slug>\", \"value\": \"<one clear sentence>\",\n"
        "             \"rationale\": \"<why, if stated, else null>\",\n"
        "             \"source\": \"state\" | \"transcript\" | \"chapter\"}.\n"
        "  open_questions (array of strings): real questions that surfaced but weren't resolved.\n"
        "      Phrased as actual questions ending in '?'. Full sentences only — not STT fragments,\n"
        "      wake words, or partial phrases like 'hey cool' or 'time right'.\n"
        "  risks (array of strings): risks/concerns that were flagged. Empty if none.\n"
        "  topics (array of strings): the discussion threads, in the order they came up. 4–10 items.\n"
        "      One short noun phrase each — 'pricing tiers', 'launch slip risk', 'hiring two engineers'.\n"
        "  meeting_tone (string): one short phrase — 'aligned', 'mostly aligned, payments still TBD',\n"
        "      'lots of open threads', 'planning session, exploratory'. Neutral and factual.\n"
        "\n"
        "Hard rules:\n"
        "- Output VALID JSON only. No markdown. No code fence. No extra text.\n"
        "- IGNORE lines tagged [assistant] or spoken by Mechus — those are AI Q&A, not team action items.\n"
        "- Never contradict structured state for owners/deadlines/decisions.\n"
        "- Long meetings: do NOT lose topics from chapters. Short meetings: keep the summary concise.\n"
        "- If the transcript truly has no commitments, return [] for action_items — do not pad.\n"
        "- executive_summary must cover the WHOLE meeting, not just the most recent part.\n"
        "- title must always be present and descriptive — never empty, never generic.\n"
    )
    state_blob = json.dumps({
        "decisions": decisions,
        "action_items": action_items,
        "open_issues": open_issues,
    }, ensure_ascii=False)
    def _call_openai(ttext_use: str, use_max_tokens: int, use_timeout: int) -> str:
        prior_block = ""
        if prior_sessions_context and prior_sessions_context.strip():
            prior_block = (
                f"\n## Prior session context (recurring meeting — use only for continuity / carryover)\n"
                f"{prior_sessions_context.strip()}\n"
            )
        user_msg = (
            f"## Structured state (authoritative — use as primary facts)\n{state_blob}\n\n"
            f"## Transcript evidence (salient + time windows; partial for very long meetings)\n{ttext_use}"
            f"{prior_block}\n\n"
            "Produce the JSON object with executive_summary, action_items, decisions, open_questions."
        )

        def _post(use_json_mode: bool) -> str:
            body: dict[str, Any] = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_msg},
                ],
                "max_tokens": use_max_tokens,
                "temperature": 0.3,
            }
            if use_json_mode:
                # gpt-4o-mini / gpt-4o support response_format=json_object.
                # Older models (e.g. gpt-3.5-turbo) return HTTP 400 — we retry
                # without it below.
                if _mechus_json_ok():
                    body["response_format"] = {"type": "json_object"}
            req = urllib.request.Request(
                _mechus_chat_url(),
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key.strip()}",
                },
                method="POST",
            )
            try:
                import certifi
                ctx = ssl.create_default_context(cafile=certifi.where())
            except ImportError:
                ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=use_timeout, context=ctx) as resp:
                return resp.read().decode("utf-8")

        try:
            return _post(use_json_mode=True)
        except urllib.error.HTTPError as exc:
            if exc.code == 400:
                log.info(
                    "Final summary: model %s rejected response_format=json_object; "
                    "retrying without JSON mode",
                    model,
                )
                return _post(use_json_mode=False)
            raise

    # Attempt 1: requested mode; attempt 2 (full only): smaller transcript + fast tokens + longer timeout
    attempts: list[tuple[str, int, int, int]] = [
        (mode, max_transcript_chars, max_tokens, eff_timeout),
    ]
    if mode == "full":
        retry_max_chars = min(max_transcript_chars, 16000)
        retry_timeout = max(eff_timeout * 2, 120)
        attempts.append(("fast", retry_max_chars, fast_max_tokens, retry_timeout))

    raw: str | None = None
    used_mode = mode
    for i, (use_mode, use_max_chars, use_max_tokens, use_timeout) in enumerate(attempts):
        slice_text = ttext[:use_max_chars] if len(ttext) > use_max_chars else ttext
        try:
            raw = _call_openai(slice_text, use_max_tokens, use_timeout)
            used_mode = use_mode
            break
        except Exception as exc:
            log.warning("Final summary LLM attempt %s failed (%s): %s", i + 1, use_mode, exc)
            if i == len(attempts) - 1:
                return _fallback_summary_from_state(
                    decisions, action_items, open_questions, transcript_text=ttext
                )

    if raw is None:
        return _fallback_summary_from_state(
            decisions, action_items, open_questions, transcript_text=ttext
        )

    payload = safe_parse_json(raw)
    if payload.get("_parse_failed"):
        log.warning("Final summary response parse failed: %s", payload.get("_parse_error"))
        return _fallback_summary_from_state(
            decisions, action_items, open_questions, transcript_text=ttext
        )

    choices = payload.get("choices") or []
    if not choices:
        return _fallback_summary_from_state(
            decisions, action_items, open_questions, transcript_text=ttext
        )

    content = (choices[0].get("message") or {}).get("content") or ""
    content = content.strip()
    # Strip markdown code fence if present
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    parsed = safe_parse_json(content)
    if parsed.get("_parse_failed"):
        return _fallback_summary_from_state(
            decisions, action_items, open_questions, transcript_text=ttext
        )

    fb_exec = _fallback_summary_from_state(
        decisions, action_items, open_questions, transcript_text=ttext
    )["executive_summary"]
    exec_in = (parsed.get("executive_summary") or "").strip()
    title_in = (parsed.get("title") or "").strip()
    norm_actions = _normalize_action_items(parsed.get("action_items"))
    state_norm = _normalize_action_items([
        {**a, "source": a.get("source") or "state"} for a in action_items
    ])
    merged_actions = _merge_action_item_sources(norm_actions, state_norm)
    # Focused second pass catches paraphrased assignments the summary pass missed.
    if api_key and mode != "state":
        try:
            extracted = extract_action_items_with_llm(
                ttext,
                known_items=merged_actions,
                participant_names=participant_names,
                api_key=api_key,
                model=model,
                timeout_seconds=min(55, max(25, eff_timeout // 2)),
                max_transcript_chars=max_transcript_chars,
            )
            merged_actions = _merge_action_item_sources(merged_actions, extracted)
        except Exception as exc:
            log.debug("Action-item enrichment skipped: %s", exc)
    out = {
        "title": title_in or _infer_title_from_topics(parsed.get("topics"), parsed.get("executive_summary")),
        "executive_summary": _coerce_executive_summary(
            exec_in,
            fallback=fb_exec,
            transcript_text=ttext,
            api_key=api_key,
            model=model,
            timeout_seconds=min(45, eff_timeout),
        ),
        "action_items": merged_actions,
        "decisions": _normalize_decisions(parsed.get("decisions")),
        "open_questions": _normalize_questions(parsed.get("open_questions")),
        "source": "llm_fast" if used_mode == "fast" else "llm",
    }
    risks = parsed.get("risks")
    if isinstance(risks, list):
        out["risks"] = _dedupe_strings(risks, min_len=4)
    topics = parsed.get("topics")
    if isinstance(topics, list):
        out["topics"] = _dedupe_strings(topics, min_len=2)
    tone = parsed.get("meeting_tone")
    if isinstance(tone, str) and tone.strip():
        out["meeting_tone"] = tone.strip()
    spoken_recap = (state.get("spoken_executive_recap") or "").strip()
    if spoken_recap and not is_executive_summary_raw_transcript_dump(spoken_recap):
        out["executive_summary"] = spoken_recap
        out["source"] = f"{out.get('source', 'llm')}+spoken_recap"
    _fb_items = _action_items_from_transcript_fallback(
        ttext,
        participant_names=participant_names or state.get("participant_names"),
    )
    if _fb_items and len(out.get("action_items") or []) < 2:
        out["action_items"] = _merge_action_item_sources(out.get("action_items") or [], _fb_items)
    return out


def _fallback_summary_from_state(
    decisions: list,
    action_items: list,
    open_questions: list,
    *,
    transcript_text: str | None = None,
) -> dict[str, Any]:
    parts: list[str] = []
    if decisions:
        d_text = "; ".join(d.get("value", "") for d in decisions[:3] if d.get("value"))
        parts.append(f"The team decided: {d_text}." if d_text else f"{len(decisions)} decision(s) recorded.")
    if action_items:
        action_lines: list[str] = []
        for a in action_items[:8]:
            owner = a.get("owner") or "Someone"
            task = a.get("task") or ""
            deadline = a.get("deadline") or a.get("due_date")
            if not task:
                continue
            line = f"{owner} — {task}"
            if deadline:
                line += f" (by {deadline})"
            action_lines.append(line)
        if action_lines:
            parts.append("Action items: " + "; ".join(action_lines) + ".")
        else:
            a_text = "; ".join(
                f"{a.get('owner', 'Someone')} will {a.get('task', '')}"
                for a in action_items[:3] if a.get("task")
            )
            parts.append(f"Next steps: {a_text}." if a_text else f"{len(action_items)} action item(s) assigned.")
    if open_questions:
        parts.append(f"Still open: {'; '.join(open_questions[:3])}.")
    executive = " ".join(parts) if parts else ""
    if not executive.strip():
        executive = _fallback_executive_summary(
            transcript_text,
            api_key=None,
        )
    # Derive a title from available state
    fb_title = _infer_title_from_topics(None, executive)
    return {
        "title": fb_title,
        "executive_summary": executive,
        "action_items": _normalize_action_items([
            {**a, "source": a.get("source") or "state"} for a in action_items
        ]),
        "decisions": _normalize_decisions(decisions),
        "open_questions": _normalize_questions(open_questions),
        "source": "fallback",
    }


def _infer_title_from_topics(topics: list | None, executive_summary: str | None) -> str:
    """Derive a short meeting title from topics or the first sentence of executive_summary.
    Used as a fallback when the LLM didn't return a title, or for state-only summaries.
    """
    if isinstance(topics, list) and topics:
        # Combine first 2 topics into a readable title
        clean = [str(t).strip() for t in topics[:2] if str(t).strip()]
        if clean:
            return " + ".join(clean).title() if len(clean) > 1 else clean[0].title()
    if executive_summary:
        # Use first sentence (up to 60 chars) as title
        first = (executive_summary.split(".")[0] or "").strip()
        if 8 < len(first) <= 60:
            return first
        if len(first) > 60:
            # Trim to last word boundary under 60 chars
            trimmed = first[:57].rsplit(" ", 1)[0]
            return trimmed + "…" if trimmed else first[:60]
    return "Meeting"


def meeting_link_key(meeting_link: str | None) -> str:
    """Stable key for a meeting link so reuses of the SAME link share memory.

    meeting_id is a fresh UUID per join, so link-scoped continuity ("this is the
    recurring standup; remember last time") needs a key derived from the link
    itself. We normalize away things that vary per-join (the bot display-name
    ``l=`` param, scheme/host case, trailing slash) and hash the rest. Same link
    in/out → same key; different meeting → different key.
    """
    raw = (meeting_link or "").strip()
    if not raw:
        return ""
    try:
        from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

        p = urlparse(raw)

        def _drop_display_name(blob: str) -> str:
            if not blob:
                return ""
            kept = [
                (k, v)
                for (k, v) in parse_qsl(blob, keep_blank_values=False)
                if k.lower() != "l"
            ]
            return urlencode(sorted(kept))

        norm = urlunparse(
            (
                (p.scheme or "").lower(),
                (p.netloc or "").lower(),
                (p.path or "").rstrip("/"),
                "",
                _drop_display_name(p.query),
                _drop_display_name(p.fragment),
            )
        )
        if not norm:
            norm = raw.lower()
    except Exception:
        norm = raw.lower()
    return hashlib.sha1(norm.encode("utf-8", "ignore")).hexdigest()[:16]


def wait_for_final_summary(
    meeting_id: str,
    *,
    base_dir: str | None = None,
    timeout_s: float = 90.0,
    poll_s: float = 2.0,
) -> dict[str, Any] | None:
    """Poll until final_summary.json exists with usable content (post docker stop)."""
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    poll_s = max(0.5, float(poll_s))
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = read_final_summary(meeting_id, base_dir=base_dir)
        if last:
            exec_sum = str(last.get("executive_summary") or "").strip()
            actions = last.get("action_items") or []
            decisions = last.get("decisions") or []
            if exec_sum or actions or decisions:
                return last
        time.sleep(poll_s)
    return last or read_final_summary(meeting_id, base_dir=base_dir)


def write_final_summary(
    meeting_id: str,
    summary: dict[str, Any],
    base_dir: str | None = None,
    *,
    meeting_link: str | None = None,
) -> str:
    """
    Write summary to {base_dir}/{meeting_id}/final_summary.json.
    Returns the path written. Uses MECHUS_MEETING_STORE_DIR if base_dir is None.

    When ``meeting_link`` is provided, stamps ``meeting_link`` + ``link_key`` so
    cross-meeting memory can find prior sessions of the SAME link.
    """
    base = _meeting_store_base(base_dir)
    out_dir = os.path.join(base, meeting_id)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "final_summary.json")
    _link = (meeting_link or os.environ.get("MECHUS_MEETING_LINK") or "").strip()
    _now = time.time()
    import datetime as _dt
    _iso = _dt.datetime.fromtimestamp(_now, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "meeting_id": meeting_id,
        "generated_at": _iso,
        "generated_at_unix": _now,
        **summary,
    }
    _title = (payload.get("title") or payload.get("meeting_name") or "").strip()
    if _title and _title.lower() not in ("meeting", "untitled meeting"):
        payload["title"] = _title
    elif not (payload.get("title") or "").strip():
        payload["title"] = _infer_title_from_topics(
            payload.get("topics"), payload.get("executive_summary")
        )
    _api_key = (os.environ.get("MECHUS_OPENAI_API_KEY") or "").strip() or None
    _model = (
        os.environ.get("MECHUS_OPENAI_THINKING_MODEL")
        or os.environ.get("MECHUS_OPENAI_CHAT_MODEL")
        or "gpt-4o-mini"
    )
    payload["executive_summary"] = _coerce_executive_summary(
        str(payload.get("executive_summary") or ""),
        fallback="",
        transcript_text=None,
        api_key=_api_key,
        model=_model,
        timeout_seconds=45,
    )
    if _link:
        payload.setdefault("meeting_link", _link)
        payload.setdefault("link_key", meeting_link_key(_link))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    log.info("Wrote final summary to %s", path)
    return path


def read_final_summary(meeting_id: str, base_dir: str | None = None) -> dict[str, Any] | None:
    """Read final_summary.json for a meeting. Returns None if missing or invalid."""
    base = _meeting_store_base(base_dir)
    path = os.path.join(base, meeting_id, "final_summary.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if isinstance(data, dict) and is_executive_summary_raw_transcript_dump(
        str(data.get("executive_summary") or "")
    ):
        _api_key = (os.environ.get("MECHUS_OPENAI_API_KEY") or "").strip() or None
        _model = (
            os.environ.get("MECHUS_OPENAI_THINKING_MODEL")
            or os.environ.get("MECHUS_OPENAI_CHAT_MODEL")
            or "gpt-4o-mini"
        )
        data["executive_summary"] = _coerce_executive_summary(
            str(data.get("executive_summary") or ""),
            fallback="",
            transcript_text=None,
            api_key=_api_key,
            model=_model,
            timeout_seconds=45,
        )
    return data
