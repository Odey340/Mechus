"""
Tier 1 (fast path) and Tier 2 (template recap) — no LLM.
Returns (answer, event_ids) for grounding. Retrieval via get_structured_qa_state(engine).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mechus.intent import (
    INTENT_ACTION_ITEMS,
    INTENT_DECISION,
    INTENT_OPEN_QUESTIONS,
    INTENT_PERSON_ACTION,
    INTENT_RECAP,
    classify_intent,
)
from mechus.json_utils import get_structured_qa_state, StructuredResult
from mechus.text import extract_requested_owner, owner_matches_item_owner

if TYPE_CHECKING:
    from mechus.context_integrity_engine import ContextIntegrityEngine


def _person_from_request(request_text: str) -> str | None:
    """Extract person name from 'what is Bob responsible for' etc."""
    if not request_text:
        return None
    t = request_text.strip().lower()
    for name in ("alice", "bob", "charlie", "dana", "carol", "dave"):
        if name in t:
            return name
    return None


def topic_from_request(request_text: str) -> str | None:
    """Infer topic namespace from question (e.g. API, pricing) for scoped decisions."""
    if not request_text:
        return None
    t = request_text.lower()
    if "api" in t or "endpoint" in t or "v1" in t or "v2" in t:
        return "api"
    if "pricing" in t or "price" in t or "tier" in t or "cost" in t:
        return "pricing"
    if "hiring" in t or "role" in t or "headcount" in t:
        return "hiring"
    if "timeline" in t or "launch" in t or "date" in t or "deadline" in t or "june" in t or "may" in t:
        return "timeline"
    if "budget" in t or "spend" in t:
        return "budget"
    if "risk" in t or "concern" in t:
        return "risk"
    if "format" in t or "elimination" in t or "round robin" in t or "bracket" in t:
        return "single"  # matches single_elimination and "single elimination"
    return None


def _template_recap(sr: StructuredResult) -> str:
    """Tier 2: build recap from structured data only (no LLM). Open + postponed only, no resolved."""
    parts = []
    if sr.decisions:
        parts.append("Decisions: " + "; ".join(v[:80] for _, v in sr.decisions[:5]))
    if sr.action_items:
        parts.append("Action items: " + " ".join(f"{o} — {t}" + (f" (due {d})" if d else "") for o, t, d in sr.action_items[:5]))
    if sr.open_questions:
        parts.append("Still open: " + "; ".join(q[:60] for q in sr.open_questions[:5]))
    if sr.postponed_questions:
        parts.append("Postponed: " + "; ".join(q[:60] for q in sr.postponed_questions[:5]))
    if not parts:
        return "I don't have enough information yet."
    return " ".join(parts)[:400].strip()


def try_structured_response(
    engine: ContextIntegrityEngine | None,
    request_text: str,
) -> tuple[str | None, list[int]]:
    """
    Tier 1 / Tier 2: deterministic or template answer and event_ids for grounding.
    Returns (answer, event_ids). answer None means fall through to Tier 3 (LLM).
    """
    if engine is None:
        return None, []
    intent = classify_intent(request_text or "")
    sr = get_structured_qa_state(engine)
    if sr.error:
        return None, []

    if intent == INTENT_ACTION_ITEMS:
        requested = extract_requested_owner(request_text or "")
        if requested:
            items = [
                (o, t, d) for o, t, d in sr.action_items
                if owner_matches_item_owner(o or "", requested)
            ]
            if not items:
                return f"No action items for {requested} yet.", sr.event_ids
            parts = [f"{owner} — {task}" + (f" (due {d})" if d else "") for owner, task, d in items]
            return " ".join(parts)[:400].strip(), sr.event_ids
        if not sr.action_items:
            return "No action items captured yet.", sr.event_ids
        parts = [f"{owner} — {task}" + (f" (due {d})" if d else "") for owner, task, d in sr.action_items]
        return " ".join(parts)[:400].strip(), sr.event_ids

    if intent == INTENT_OPEN_QUESTIONS:
        if not sr.open_questions and not sr.postponed_questions:
            return "No open questions recorded.", sr.event_ids
        parts = []
        if sr.open_questions:
            parts.append("Still open: " + "; ".join(q[:80] for q in sr.open_questions[:10]))
        if sr.postponed_questions:
            parts.append("Postponed: " + "; ".join(q[:80] for q in sr.postponed_questions[:5]))
        return " ".join(parts)[:400].strip(), sr.event_ids

    if intent == INTENT_DECISION:
        decisions = sr.decisions
        topic = topic_from_request(request_text)
        if topic:
            decisions = [(did, val) for did, val in decisions if topic in did.lower() or topic in val.lower()]
        if not decisions:
            return ("No decision captured for that yet." if topic else "No decisions captured yet."), sr.event_ids
        if len(decisions) == 1:
            return decisions[0][1][:200].strip(), sr.event_ids
        return " ".join(f"{did}: {val[:60]}" for did, val in decisions)[:400].strip(), sr.event_ids

    if intent == INTENT_PERSON_ACTION:
        person = extract_requested_owner(request_text or "") or _person_from_request(request_text)
        if not person:
            return None, []
        items = [
            (o, t, d) for o, t, d in sr.action_items
            if owner_matches_item_owner(o or "", person)
        ]
        if not items:
            display = person.title() if person.islower() else person
            return f"No action items for {display} yet.", sr.event_ids
        parts = [f"{o} — {t}" + (f" (by {d})" if d else "") for o, t, d in items]
        return " ".join(parts)[:400].strip(), sr.event_ids

    # Tier 2: simple recap from structured data only
    if intent == INTENT_RECAP and (sr.decisions or sr.action_items or sr.open_questions or sr.postponed_questions):
        return _template_recap(sr), sr.event_ids

    return None, []
