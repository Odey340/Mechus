"""
Retrieval layer: for a user question, return only the relevant tagged chunks.

Source-of-truth hierarchy for the LLM (see also mechus/responder.py):
  1. Canonical state from ContextIntegrityEngine (decisions, action_items, open_issues) — structured facts.
  2. MEETING_SNIPPETS from get_relevant_chunks — what was said (tagged transcript excerpts).
  3. memory.retrieve() — auxiliary dialog/topic retrieval; not primary for commitments.

Transcript is chunked and tagged in the background (topic + type: decision, action_item, open_question).
On question we retrieve only those chunks that match the question → send only that to the LLM.
"""

from __future__ import annotations
import re

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mechus.intent import INTENT_GENERAL, INTENT_RECAP, classify_intent
from mechus.dialog_memory import (
    is_dialog_continuation,
    is_prior_exchange_reference,
    is_recent_auditory_query,
    is_vague_followup,
)

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from mechus.context_integrity_engine import ContextIntegrityEngine
    from mechus.memory import ConversationMemory


@dataclass
class RetrievalBundle:
    """Context for the LLM and responder. State is canonical (decisions, action_items, issues)."""
    formatted_context: str  # Tagged chunks relevant to the question (for LLM prompt)
    memory_text: str
    topic: str | None
    event_ids: list[int]
    intent: str
    # Tagged text chunks (legacy fallback when state is insufficient)
    chunks: list[dict]
    # Canonical state for state-rendering: {decisions, action_items, open_issues}. Responder renders this, does not re-infer from text.
    state: dict
    # Observability: engine chunk path vs light/general path, character counts
    meta: dict = field(default_factory=dict)


def _extract_freeform_topic_hint(request_text: str) -> str | None:
    """Noun or token after 'about' / 'regarding' / 'what we said about' for scoped recap."""
    t = (request_text or "").strip()
    if not t:
        return None
    m = re.search(
        r"\b(?:about|regarding|concerning)\s+([A-Za-z0-9][A-Za-z0-9\-]{1,48})\b",
        t,
        re.I,
    )
    if m:
        hint = m.group(1).strip()
        if len(hint) >= 2 and hint.lower() not in {
            "the", "our", "your", "this", "that", "what", "how", "all", "any",
        }:
            return hint
    m = re.search(
        r"\bwhat\s+(?:did|have)\s+we\s+say\s+about\s+([A-Za-z0-9][A-Za-z0-9\-]{1,48})\b",
        t,
        re.I,
    )
    if m:
        return m.group(1).strip()
    return None


def _recap_uses_standard_path(intent: str, topic_bucket: str | None, request_text: str) -> bool:
    """Use BM25/hybrid chunk ranking instead of chapter+tail when recap is topic-scoped."""
    if intent != INTENT_RECAP:
        return False
    if _extract_freeform_topic_hint(request_text):
        return True
    if topic_bucket is not None and topic_bucket != "recap":
        return True
    return False


def _recap_scope(
    intent: str,
    time_window_minutes: float | None,
    recap_use_standard: bool,
) -> str:
    if intent != INTENT_RECAP:
        return "none"
    if time_window_minutes is not None:
        return "window"
    if recap_use_standard:
        return "topic"
    return "full"


def retrieve_for_question(
    request_text: str,
    *,
    retrieval_query: str | None = None,
    salient_terms_used: list[str] | None = None,
    context_engine: "ContextIntegrityEngine | None" = None,
    memory: "ConversationMemory | None" = None,
    recent_transcript: str = "",
    shared_context_text: str = "",
    max_chars: int = 4000,
    general_max_chars: int | None = None,
    retrieval_mode: str = "lexical",
    bm25_weight: float = 0.45,
    tag_weight: float = 0.55,
    embed_weight: float = 0.32,
    embed_rerank_top_k: int = 28,
    openai_api_key: str | None = None,
    embedding_model: str = "text-embedding-3-small",
    retrieval_auto_lexical_gap_min: float = 0.12,
    retrieval_auto_top_floor_hybrid: float = 0.18,
    query_embed_cache_max: int = 128,
    meeting_now_ts: float | None = None,
    retrieval_extra_meta: dict | None = None,
    dialog_last_user: str | None = None,
    dialog_last_mechus: str | None = None,
) -> RetrievalBundle:
    """
    Retrieve only the chunks relevant to this question (by tags + topic).
    The LLM receives just these chunks, not the whole meeting.

    For INTENT_GENERAL we still attach canonical state + a smaller engine retrieval budget
    so answers do not contradict structured meeting facts.

    meeting_now_ts: optional "current meeting time" in the same coordinate system as
    ingest_chunk(..., timestamp=...). Used for "recap the last N minutes" filtering.
    When omitted, time.time() is used (wall clock; matches tests that patch time.time).

    retrieval_query: optional expanded string for chunk + memory retrieval only.
    Intent classification, topic hints, and time-window parsing always use request_text.

    retrieval_extra_meta: optional dict merged into bundle.meta (e.g. cache_skipped, salient lists from agent).
    """
    request_text = (request_text or "").strip()
    q = (retrieval_query if retrieval_query is not None else request_text).strip()
    intent = classify_intent(request_text)
    topic = _topic_from_request(request_text)
    time_window_seconds = _extract_time_window_seconds(request_text)
    time_window_minutes = _extract_time_window_minutes(request_text)
    if time_window_seconds is None and is_recent_auditory_query(request_text):
        time_window_seconds = 30.0
    engine_topic = None if topic in (None, "recap") else topic
    recap_use_standard = _recap_uses_standard_path(intent, topic, request_text)
    recap_scope = _recap_scope(intent, time_window_minutes, recap_use_standard)
    freeform_topic = _extract_freeform_topic_hint(request_text)
    effective_request = f"{q} {freeform_topic}".strip() if freeform_topic else q
    memory_text = ""
    if memory:
        memory_text = memory.retrieve(request_text=q)

    formatted_context = ""
    event_ids: list[int] = []
    chunks: list[dict] = []
    state: dict = {}
    meta: dict = {
        "intent": intent,
        "used_light_path": False,
        "engine_snippet_chars": 0,
        "formatted_context_chars": 0,
        "memory_retrieve_chars": len(memory_text or ""),
        "retrieval_mode": retrieval_mode,
        "effective_retrieval_mode": None,
        "lexical_confidence": None,
        "hybrid_skipped_reason": "",
        "retrieval_ms": 0.0,
        "selected_chunk_ids": [],
        "embed_rerank_used": False,
        "time_window_minutes": time_window_minutes,
        "recap_scope": recap_scope,
        "recap_freeform_topic": freeform_topic,
        "recap_use_standard_path": recap_use_standard,
        "meeting_now_ts": meeting_now_ts,
        "recap_reference_now_ts": None,
        "retrieval_query_expanded": q != request_text,
        "retrieval_effective_query": q,
        "salient_terms_used": list(salient_terms_used or []),
        "request_text": request_text,
        "vague_followup": is_vague_followup(request_text),
        "prior_exchange_reference": is_prior_exchange_reference(request_text),
        "dialog_continuation": is_dialog_continuation(request_text),
        "recent_auditory_query": is_recent_auditory_query(request_text),
        "time_window_seconds": time_window_seconds,
        "topic_derived_from_request_text": True,
    }

    _vague = bool(meta["vague_followup"])
    _prior = bool(meta["prior_exchange_reference"])
    _cont = bool(meta["dialog_continuation"])
    _wake_followup = (retrieval_extra_meta or {}).get("wake_source") == "transcript_followup"
    _anchor_dialog = _vague or _prior or _cont or _wake_followup
    _recency_w = 0.32 if (_cont or _prior) else (0.28 if _anchor_dialog else (0.14 if intent == INTENT_GENERAL else 0.0))
    _tail_chunks = 12 if _cont else (8 if _anchor_dialog else (4 if intent == INTENT_GENERAL else 0))

    _context_instruction = (
        "This is the transcript from the current meeting session. "
        "Use it for anything about what was said, decisions, or action items here. "
        "You may also draw on general knowledge when the user asks for facts or background that go beyond the transcript "
        "(e.g. explaining a place, school, product, or concept they mentioned). "
        "Do not treat other meetings' data as fact for this live meeting unless it is explicitly provided "
        "under TEAM_SHARED_CONTEXT and the user asks about team, historical, or shared context. "
        "Interpret likely speech-recognition errors charitably using transcript and topic.\n\n"
    )

    use_light_path = intent == INTENT_GENERAL
    gmax = general_max_chars if general_max_chars is not None else min(max_chars, 3200)

    if context_engine is not None:
        state = context_engine.get_canonical_state_for_qa()
        chunk_budget = gmax if use_light_path else max_chars
        rmeta: dict = {}
        t0 = time.perf_counter()
        chunk_context, event_ids, tagged_list = context_engine.get_relevant_chunks(
            effective_request,
            intent=intent,
            topic=engine_topic,
            max_chars=chunk_budget,
            retrieval_mode=retrieval_mode,
            bm25_weight=bm25_weight,
            tag_weight=tag_weight,
            embed_weight=embed_weight,
            embed_rerank_top_k=embed_rerank_top_k,
            openai_api_key=openai_api_key,
            embedding_model=embedding_model,
            retrieval_auto_lexical_gap_min=retrieval_auto_lexical_gap_min,
            retrieval_auto_top_floor_hybrid=retrieval_auto_top_floor_hybrid,
            query_embed_cache_max=query_embed_cache_max,
            out_meta=rmeta,
            recap_use_standard_path=recap_use_standard,
            recency_weight=_recency_w,
            ensure_recent_tail_chunks=_tail_chunks,
        )
        # Filter to time window if requested (e.g. "recap the last 5 minutes").
        # For time-based recaps, bypass relevance scoring and use ALL chunks in
        # the window in chronological order. The reference "now" is the most
        # recent ingested chunk timestamp (meeting-relative), NOT wall-clock —
        # so a window measured right after the user's request lines up with what
        # was actually said, even if the meeting clock differs from real time.
        windowed_recap = False
        _window_s = time_window_seconds
        if _window_s is None and time_window_minutes is not None:
            _window_s = time_window_minutes * 60.0
        if _window_s is not None and context_engine is not None:
            now_ts = meeting_now_ts if meeting_now_ts is not None else time.time()
            # Guard against a stale reference: if the latest stored chunk is
            # newer than the passed-in now (e.g. the request line was ingested
            # after now_ts was sampled), anchor to the newest chunk so the
            # window includes everything up to the moment of asking.
            timestamps = context_engine._chunk_timestamps
            if timestamps:
                now_ts = max(now_ts, timestamps[-1])
            meta["recap_reference_now_ts"] = now_ts
            cutoff_ts = now_ts - _window_s
            windowed_recap = True
            # Use ALL tagged chunks in the window, not just relevance-scored ones
            time_filtered = []
            for tc in context_engine._tagged_chunks:
                idx = tc.chunk_id - 1
                if 0 <= idx < len(timestamps):
                    if timestamps[idx] >= cutoff_ts:
                        time_filtered.append(tc)
            meta["time_window_chunk_count"] = len(time_filtered)
            _win_label = (
                _format_seconds_label(_window_s)
                if time_window_seconds is not None or is_recent_auditory_query(request_text)
                else _format_window_label(time_window_minutes or (_window_s / 60.0))
            )
            if time_filtered:
                tagged_list = time_filtered
                # Build full chronological context — include everything said in window
                chunk_context = "\n".join(
                    f"[{tc.speaker_id}] {tc.text}"
                    for tc in sorted(time_filtered, key=lambda x: x.chunk_id)
                )
                formatted_context = (
                    f"Meeting transcript — last {_win_label}:\n\n"
                    + _context_instruction
                    + chunk_context
                )
            else:
                # Nothing was captured in the requested window. Tell the user
                # plainly instead of silently recapping the entire meeting
                # (which is what made short-window recaps feel "iffy").
                tagged_list = []
                chunk_context = ""
                formatted_context = (
                    f"(No substantive discussion was captured in the last {_win_label}. "
                    "If asked for a recap of this window, say nothing notable happened in "
                    "that time rather than summarizing earlier parts of the meeting.)"
                )
                meta["time_window_empty"] = True

        meta["retrieval_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        meta.update(rmeta)
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "retrieve_for_question intent=%s mode=%s chunk_ids=%s embed_rerank=%s ms=%s",
                intent,
                meta.get("retrieval_mode"),
                meta.get("selected_chunk_ids"),
                meta.get("embed_rerank_used"),
                meta.get("retrieval_ms"),
            )
        meta["used_light_path"] = bool(use_light_path)
        meta["engine_chunk_chars"] = len(chunk_context or "")
        meta["engine_snippet_chars"] = meta["engine_chunk_chars"]
        # A time-windowed recap already built its own framed context above; do not
        # overwrite it with the generic header (that dropped the "last N minutes"
        # scope) nor append recent lines that may fall outside the window.
        if chunk_context and not windowed_recap:
            formatted_context = "Meeting transcript (this session):\n\n" + _context_instruction + chunk_context
        _dlg_user = (dialog_last_user or "").strip()
        _dlg_mechus = (dialog_last_mechus or "").strip()
        if (_dlg_user or _dlg_mechus) and _anchor_dialog and not windowed_recap:
            exchange_lines: list[str] = []
            if _dlg_user:
                exchange_lines.append(f"User: {_dlg_user[:400]}")
            if _dlg_mechus:
                exchange_lines.append(f"Mechus: {_dlg_mechus[:500]}")
            if _cont:
                _lead = (
                    "Current exchange — the user's latest line continues this thread; "
                    "stay on this topic, not unrelated earlier meeting discussion:\n"
                )
            else:
                _lead = (
                    "Current exchange — answer in context of this turn; "
                    "do not pull unrelated earlier meeting topics:\n"
                )
            exchange_block = _lead + "\n".join(exchange_lines)
            if formatted_context and formatted_context != "(No meeting context yet.)":
                formatted_context = exchange_block + "\n\n" + formatted_context.rstrip()
            else:
                formatted_context = exchange_block
        if recent_transcript and not windowed_recap:
            formatted_context = (formatted_context + "\n\nRecent:\n" + recent_transcript).strip()
        chunks = [
            {"speaker": tc.speaker_id, "text": tc.text, "tags": list(tc.tags)}
            for tc in tagged_list
        ]
    else:
        if recent_transcript or memory_text:
            formatted_context = (recent_transcript or "") + "\n\n" + (memory_text or "")
            formatted_context = ("Meeting transcript (this session):\n\n" + _context_instruction + formatted_context).strip()

    if not formatted_context:
        formatted_context = "(No meeting context yet.)"

    shared = (shared_context_text or "").strip()
    if shared:
        shared_block = (
            "=== TEAM_SHARED_CONTEXT (opt-in prior team meetings and notes; not necessarily from this live meeting) ===\n"
            + shared
        )
        if formatted_context == "(No meeting context yet.)":
            formatted_context = shared_block
        else:
            formatted_context = formatted_context.rstrip() + "\n\n" + shared_block

    meta["formatted_context_chars"] = len(formatted_context)
    meta["team_shared_context_chars"] = len(shared)

    if retrieval_extra_meta:
        meta.update(retrieval_extra_meta)

    return RetrievalBundle(
        formatted_context=formatted_context,
        memory_text=memory_text,
        topic=topic,
        event_ids=event_ids,
        intent=intent,
        chunks=chunks,
        state=state,
        meta=meta,
    )


# Spoken STT often emits words ("five") not digits ("5"); keep in sync with intent time-window recap.
_MINUTES_WORD_TO_NUM: dict[str, float] = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "forty-five": 45,
    "twenty-five": 25,
}


def _minutes_from_spoken_token(token: str) -> float | None:
    w = token.lower().strip()
    return _MINUTES_WORD_TO_NUM.get(w)


def _extract_time_window_seconds(request_text: str) -> float | None:
    """Extract a sub-minute window in seconds (e.g. 'last 10 seconds')."""
    t = (request_text or "").lower()
    m = re.search(r"last\s+(\d+)\s*seconds?", t)
    if m:
        return float(m.group(1))
    m = re.search(r"past\s+(\d+)\s*seconds?", t)
    if m:
        return float(m.group(1))
    return None


def _format_seconds_label(seconds: float) -> str:
    n = int(round(seconds))
    if n <= 1:
        return "second"
    return f"{n} seconds"


def _format_window_label(minutes: float) -> str:
    """Human label for a recap window, e.g. 1 -> 'minute', 5 -> '5 minutes', 60 -> 'hour'."""
    if minutes >= 60 and minutes % 60 == 0:
        hrs = int(minutes // 60)
        return "hour" if hrs == 1 else f"{hrs} hours"
    if abs(minutes - round(minutes)) < 1e-6:
        n = int(round(minutes))
        return "minute" if n == 1 else f"{n} minutes"
    return f"{minutes:g} minutes"


def _extract_time_window_minutes(request_text: str) -> float | None:
    """Extract time window in minutes from request. Returns None if no time reference found."""
    t = request_text.lower()
    # "last X minutes/mins/min" (digits)
    m = re.search(r"last\s+(\d+)\s*(?:minutes?|mins?)", t)
    if m:
        return float(m.group(1))
    # "last X minutes" (spoken: five, two, one, …)
    m = re.search(r"last\s+([a-z]+(?:-[a-z]+)?)\s*(?:minutes?|mins?)\b", t)
    if m:
        n = _minutes_from_spoken_token(m.group(1))
        if n is not None:
            return n
    # "last X hours" (digits)
    m = re.search(r"last\s+(\d+)\s*(?:hours?|hrs?)", t)
    if m:
        return float(m.group(1)) * 60
    # "last X hours" (spoken)
    m = re.search(r"last\s+([a-z]+(?:-[a-z]+)?)\s*(?:hours?|hrs?)\b", t)
    if m:
        n = _minutes_from_spoken_token(m.group(1))
        if n is not None:
            return n * 60.0
    # "last half hour"
    if "last half hour" in t or "past half hour" in t:
        return 30.0
    # "last hour"
    if re.search(r"last\s+(?:an\s+)?hour\b", t):
        return 60.0
    # "past X minutes" (digits)
    m = re.search(r"past\s+(\d+)\s*(?:minutes?|mins?)", t)
    if m:
        return float(m.group(1))
    # "past X minutes" (spoken)
    m = re.search(r"past\s+([a-z]+(?:-[a-z]+)?)\s*(?:minutes?|mins?)\b", t)
    if m:
        n = _minutes_from_spoken_token(m.group(1))
        if n is not None:
            return n
    return None


def _topic_from_request(request_text: str) -> str | None:
    """Infer topic for scoped retrieval (same logic as structured_response)."""
    if not request_text:
        return None
    t = request_text.lower()
    if "api" in t or "endpoint" in t or "v1" in t or "v2" in t:
        return "api"
    if "pricing" in t or "price" in t or "tier" in t:
        return "pricing"
    if "cloud" in t or "infrastructure" in t or "environment" in t or "load test" in t:
        return "infrastructure"
    if "cost" in t or "budget" in t or "spend" in t or "burn" in t or "overrun" in t or "financial" in t or "number" in t:
        return "budget"
    if "hiring" in t or "role" in t or "headcount" in t:
        return "hiring"
    if "timeline" in t or "launch" in t or "date" in t or "deadline" in t or "go-live" in t or "milestone" in t or "freeze" in t:
        return "timeline"
    if "risk" in t or "concern" in t or "blocker" in t or "mitigation" in t:
        return "risk"
    if "sprint" in t or "velocity" in t or "story" in t or "point" in t or "capacity" in t:
        return "sprint"
    if "action" in t or "who owns" in t or "assigned" in t or "responsible" in t:
        return "action"
    if "vendor" in t or "sdk" in t or "escalat" in t or "third.party" in t:
        return "vendor"
    if "engineer" in t or "developer" in t or "team" in t or "resource" in t:
        return "capacity"
    if "decision" in t or "decided" in t or "agree" in t or "locked" in t:
        return "decision"
    if "recap" in t or "summary" in t or "overview" in t or "meeting" in t:
        return "recap"
    if "supplier" in t or "contract" in t or "vendor" in t or "sourcing" in t:
        return "vendor"
    if "menu" in t or "item" in t or "food" in t or "recipe" in t or "dish" in t:
        return "menu"
    if "marketing" in t or "campaign" in t or "social" in t or "creative" in t or "brand" in t:
        return "marketing"
    if "training" in t or "staff" in t or "onboard" in t:
        return "training"
    if "pricing" in t or "price" in t or "cost" in t or "margin" in t or "combo" in t:
        return "pricing"
    if "vegan" in t or "vegetarian" in t or "plant" in t:
        return "menu"
    if "kids" in t or "children" in t or "redesign" in t:
        return "menu"
    if "format" in t or "elimination" in t or "round robin" in t or "bracket" in t:
        return "single"
    return None
