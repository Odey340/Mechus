"""
Context Integrity Engine.

Processes meeting transcript in chronological chunks. Maintains persistent
structured memory. Does NOT summarize until explicitly instructed with:
"END OF MEETING — GENERATE OUTPUT"

Production-oriented behavior:
- Stateful decisions (id, current_value, history, reversed) — not flat text.
- Topic partitioning (pricing, api, hiring, timeline, budget, risk, action_item).
- Conflict graph for contradictions (e.g. budget 50K vs 40K).
- Stricter action items (owner in participants, verb phrase, no open-questions as actions).
- Timeline includes reversal entries; QA API for focused retrieval.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

from mechus.bm25 import bm25_scores, min_max_norm
from mechus.embeddings_util import cosine_similarity, fetch_embeddings
from mechus.intent import INTENT_GENERAL

# End-of-meeting trigger (exact match after normalization)
END_OF_MEETING_TRIGGER = "end of meeting — generate output"

# Substrings that suggest paraphrase / theme-style questions → prefer hybrid in auto mode.
_VAGUE_HYBRID_SUBSTR = (
    "theme",
    "themes",
    "overall",
    "concern",
    "worries",
    "worry",
    "big picture",
    "summarize",
    "summary",
    "tone of",
    "main takeaway",
    "what kept",
    "vague",
)


def _vague_hybrid_signal(request_text: str) -> bool:
    t = (request_text or "").lower()
    return any(s in t for s in _VAGUE_HYBRID_SUBSTR)


def _effective_retrieval_for_auto(
    intent: str,
    request_text: str,
    combined: list[float],
    full_order: list[int],
    n: int,
    *,
    gap_min_lexical: float,
    top_floor_hybrid: float,
) -> tuple[str, float, str]:
    """
    Decide lexical vs hybrid when retrieval_mode=auto.
    Returns (effective_mode, lexical_confidence_gap, reason_code).
    """
    if n < 2:
        return "hybrid", 0.0, "single_chunk"
    top_score = combined[full_order[0]]
    gap = combined[full_order[0]] - combined[full_order[1]]
    if intent == INTENT_GENERAL:
        return "hybrid", gap, "intent_general_semantic"
    if _vague_hybrid_signal(request_text):
        return "hybrid", gap, "vague_query_phrase"
    if top_score < top_floor_hybrid:
        return "hybrid", gap, "weak_top_score"
    if gap < gap_min_lexical:
        return "hybrid", gap, "low_lexical_separation"
    return "lexical", gap, "high_lexical_confidence"

# Topic namespaces for partitioning
TOPIC_NAMESPACES = (
    "pricing",
    "api",
    "hiring",
    "timeline",
    "budget",
    "risk",
    "action_item",
)

# Keywords per topic (lowercase) for classification
_TOPIC_KEYWORDS: dict[str, set[str]] = {
    "pricing": {"pricing", "price", "tier", "tiers", "fee", "quote", "discount", "revenue", "margin", "charge"},
    "api": {"api", "endpoint", "v1", "v2", "version", "integration", "rate limit", "rate-limit", "sdk", "webhook"},
    "hiring": {"hiring", "hire", "role", "headcount", "candidate", "interview", "job", "recruit", "onboard"},
    "timeline": {"timeline", "launch", "date", "deadline", "quarter", "week", "month", "milestone", "go-live", "freeze", "deploy", "release", "ship"},
    "budget": {"budget", "spend", "allocation", "cost", "approval", "overrun", "burn", "dollar", "financial", "forecast", "savings", "expense", "k", "m"},
    "risk": {"risk", "concern", "worried", "dependency", "blocker", "assumption", "vulnerability", "mitigation", "impact", "probability"},
    "action_item": {"action", "will", "own", "due", "follow up", "next step", "responsible", "assigned", "owner"},
    "infrastructure": {"cloud", "environment", "server", "container", "docker", "deployment", "infrastructure", "load test", "instance", "devops"},
    "sprint": {"sprint", "velocity", "story point", "backlog", "capacity", "utilization", "iteration", "scrum", "standup"},
    "vendor": {"vendor", "supplier", "sdk", "third party", "escalate", "account manager", "contract", "agreement"},
    "capacity": {"engineer", "developer", "team", "resource", "availability", "sick", "conference", "headcount", "staffing"},
    "product": {"feature", "product", "module", "component", "integration", "functionality", "requirement", "spec"},
    "decision": {"decided", "agreed", "locked", "confirmed", "finalized", "settled", "committed"},
    "action_item": {"action", "will", "own", "due", "follow up", "next step"},
    "general": {"discuss", "talk", "mention", "said", "think", "feel", "believe", "agree", "disagree", "argue", "debate", "point", "fact", "interesting", "important"},
}


@dataclass
class ActionItemRecord:
    owner: str
    task: str
    deadline: Optional[str]
    origin_ts: float
    chunk_id: int
    confidence: str  # "high" | "medium" | "low"


@dataclass
class DecisionRecord:
    """Legacy flat record; kept for backward compat. Prefer DecisionState."""
    decision: str
    origin_ts: float
    chunk_id: int
    reversed_by_chunk_id: Optional[int] = None
    confidence: str = "high"


@dataclass
class DecisionState:
    """Stateful decision: current value, history, and reversal status."""
    id: str  # e.g. "api_strategy", "pricing_tier"
    current_value: str
    history: list[tuple[int, str]]  # (chunk_id, value) — prior values including reversed
    reversed: bool
    owner: str
    last_updated_chunk: int
    origin_ts: float = 0.0
    confidence: str = "high"


@dataclass
class ConflictRecord:
    """Structured conflict: same concept, different values (e.g. budget 50K vs 40K)."""
    name: str  # e.g. "budget", "launch_date"
    values_mentioned: list[tuple[str, int, str]]  # (value, chunk_id, speaker_id) 
    status: str  # "unresolved" | "resolved"


# Issue lifecycle: open | resolved | postponed (rules-based, no embeddings)
ISSUE_STATUS_OPEN = "open"
ISSUE_STATUS_RESOLVED = "resolved"
ISSUE_STATUS_POSTPONED = "postponed"


@dataclass
class Issue:
    """Tracked open question / issue with lifecycle (open → resolved or postponed)."""
    issue_id: str
    topic_key: str  # normalized: format, marketing_deadline, livestream, referees, etc.
    text: str  # original question text (for debugging; not shown in "Still open")
    status: str  # open | resolved | postponed
    opened_chunk_id: int
    resolved_chunk_id: Optional[int] = None
    evidence_chunk_ids: list[int] = field(default_factory=list)  # for grounding
    display_summary: str = ""  # human-readable for answers, e.g. "securing four certified referees"


@dataclass
class FactRecord:
    fact: str
    speaker_id: str
    origin_ts: float
    chunk_id: int
    confidence: str
    superseded_by_chunk_id: Optional[int] = None
    topic: str = "general"  # one of TOPIC_NAMESPACES or "general"


@dataclass
class TaggedChunk:
    """One transcript chunk with tags for retrieval (topic + type)."""
    chunk_id: int
    text: str
    speaker_id: str
    topic: str  # from _classify_topic
    tags: list[str]  # e.g. ["decision"], ["action_item"], ["open_question"]


@dataclass
class MemoryUpdate:
    """Response for each chunk (no summarization)."""
    new_facts: list[str]
    updated_facts: list[str]
    conflicts_detected: list[str]
    action_items_added: list[str]
    decisions_updated: list[str]
    open_loops_remaining: list[str]


@dataclass
class FinalReport:
    executive_summary: str
    final_decisions: list[str]
    action_items_table: list[tuple[str, str, Optional[str], float]]
    contradictions_identified: list[str]
    risks_identified: list[str]
    unresolved_questions: list[str]
    speaker_alignment_matrix: list[tuple[str, str, str]]
    timeline_decision_points: list[tuple[float, str]]
    logic_inconsistencies: list[str]
    confidence_score: int
    confidence_reasoning: str


def _normalize_trigger(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _is_end_of_meeting(chunk_text: str) -> bool:
    return END_OF_MEETING_TRIGGER in _normalize_trigger(chunk_text)


# ---- Extraction (rule-based to avoid inventing; low confidence when ambiguous) ----

# Names that are clearly not owners (pronouns, common words)
_OWNER_STOPWORDS = {
    "we", "i", "they", "it", "someone", "everyone", "the", "a", "an",
    "need", "actually", "decide", "figure", "use", "revert", "s", "no", "change",
    "still", "have", "decided",
}

_ACTION_PATTERNS = [
    # "Action item: Alice will finalize the release notes by June 1" or "Alice will ... by June 1"
    re.compile(
        r"action\s*(?:item)?\s*[:\-]?\s*([A-Za-z][A-Za-z\s]{1,30}?)\s+will\s+([^.;]+?)(?:\s+by\s+([^.;]+?))?(?:\.|;|$)",
        re.IGNORECASE,
    ),
    # "X will Y" / "X needs to Y" / "X should Y" with X a name-like token (no "We need", "Actually we")
    re.compile(
        r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+(?:will|to|should|has to|needs to|need to)\s+([^.;]{10,}?)(?:\.|;|$)",
        re.IGNORECASE,
    ),
    # "I have X due by Y" — owner is speaker
    re.compile(
        r"I\s+have\s+([^.;]{10,}?)\s+due\s+(?:at|by|on)?\s*([^.;]{5,}?)(?:\.|;|$)",
        re.IGNORECASE,
    ),
    # "X is due on Y" / "X due by Y" — no explicit owner, assign to speaker
    re.compile(
        r"([^.;]{10,}?)\s+(?:is\s+)?due\s+(?:at|by|on)\s+([^.;]{5,}?)(?:\.|;|$)",
        re.IGNORECASE,
    ),
    # "Carol will own the marketing rollout. Due by end of May."
    re.compile(
        r"\b([A-Z][a-z]+)\s+will\s+own\s+([^.;]+?)(?:\.|;|due\s+by\s+([^.;]+?))?(?:\.|;|$)",
        re.IGNORECASE,
    ),
    # "I'll book Lincoln High by this Friday" — owner = current speaker
    re.compile(
        r"(?:I'll|I will)\s+([^.;]{10,}?)(?:\s+by\s+([^.;]+?))?(?:\.|;|$)",
        re.IGNORECASE,
    ),
    # "Tomás — supplier contracts — May 20th" style informal assignments
    re.compile(
        r"\b([A-Z][a-z]+)\s*[—\-]\s*([^—\-\n]{10,}?)\s*[—\-]\s*([^—\-\n]{5,}?)(?:\.|;|$)",
        re.IGNORECASE,
    ),
    # "X owns that / X handles that / X is on that"
    re.compile(
        r"\b([A-Z][a-z]+)\s+(?:owns|handles|is on|takes care of|is responsible for)\s+([^.;]{10,}?)(?:\.|;|$)",
        re.IGNORECASE,
    ),
    # "I need X done by Y" — owner is speaker
    re.compile(
        r"I\s+need\s+([^.;]{10,}?)\s+(?:done|completed|finished|ready|submitted)\s+by\s+([^.;]{5,}?)(?:\.|;|$)",
        re.IGNORECASE,
    ),
    # Passive assignments: "X has been assigned Y" / "X was assigned Y"
    re.compile(
        r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+(?:has\s+been|have\s+been|is\s+now)\s+assigned\s+(?:to\s+)?([^.;]{10,}?)(?:\.|;|$)",
        re.IGNORECASE,
    ),
    # "task/work assigned to X" — reverse: g[0]=task phrase, g[1]=owner name
    re.compile(
        r"\b(the\s+[^.;]{5,50}?)\s+(?:is\s+|was\s+|has\s+been\s+)?assigned\s+to\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)(?:\.|;|$)",
        re.IGNORECASE,
    ),
    # "we need/want/asked X to do Y" — X is proper name
    re.compile(
        r"\bwe\s+(?:need|want|asked|ask)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+to\s+([^.;]{10,}?)(?:\.|;|$)",
        re.IGNORECASE,
    ),
    # "X has been asked to Y" / "X was asked to Y"
    re.compile(
        r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+(?:has\s+been|was)\s+asked\s+to\s+([^.;]{10,}?)(?:\.|;|$)",
        re.IGNORECASE,
    ),
    # "that's on X to Y" / "it's on X to Y" / "this is on X"
    re.compile(
        r"(?:that'?s|it'?s|this\s+is|that\s+is)\s+on\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b\s*(?:to\s+([^.;]{8,}?))?(?:\.|;|$)",
        re.IGNORECASE,
    ),
    # "I'm assigning X to handle Y" / "we're assigning X to Y"
    re.compile(
        r"(?:i'?m|we'?re|i\s+am|we\s+are)\s+assigning\s+(?:this\s+to\s+)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+(?:to\s+|for\s+)?([^.;]{10,}?)(?:\.|;|$)",
        re.IGNORECASE,
    ),
    # "X is taking / X will be taking / X takes" this/that/the …
    re.compile(
        r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+(?:is\s+taking|will\s+be\s+taking|takes)\s+(?:care\s+of\s+|over\s+)?([^.;]{10,}?)(?:\.|;|$)",
        re.IGNORECASE,
    ),
    # "I'm going to / I am going to" — owner is speaker
    re.compile(
        r"(?:I'?m\s+going\s+to|I\s+am\s+going\s+to|I\s+plan\s+to|I\s+intend\s+to)\s+([^.;]{10,}?)(?:\.|;|$)",
        re.IGNORECASE,
    ),
]

_DEADLINE_PATTERN = re.compile(
    r"(?:due|deadline|by|before)\s+(?:date\s*)?[:\s]*"
    r"(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}|\d{1,2}\s+\w+\s+\d{2,4}|"
    r"\w+\s+\d{1,2}(?:st|nd|rd|th)?|next\s+\w+|end\s+of\s+(?:\w+|the\s+\w+)|"
    r"(?:this|next)\s+(?:monday|tuesday|wednesday|thursday|friday|week|month)|eod|eow|eom)",
    re.IGNORECASE,
)

_DECISION_PATTERNS = [
    re.compile(r"(?:we\s+)(?:have\s+)?(?:decided|agreed|agreed to)\s+[:\s]*([^.;]{10,})", re.IGNORECASE),
    re.compile(r"decision\s*[:\-]\s*([^.;\n]{10,})", re.IGNORECASE),
    re.compile(r"decision\s+made\s*[—\-]\s*([^.;\n]{10,})", re.IGNORECASE),
    re.compile(r"(?:so\s+)?(?:the\s+)?(?:final\s+)?decision\s+is\s+[:\s]*([^.;]{10,})", re.IGNORECASE),
    re.compile(r"let'?s\s+(?:go\s+)?with\s+([^.;]{10,})", re.IGNORECASE),
    re.compile(r"(?:that'?s\s+)?(?:a\s+)?decision\s*[—\-:]\s*(?:we'?re\s+)?([^.;]{10,})", re.IGNORECASE),
    re.compile(r"we'?re\s+(?:going\s+to\s+|going\s+ahead\s+with\s+|committing\s+to\s+)([^.;]{10,})", re.IGNORECASE),
    re.compile(r"(?:let'?s\s+)?(?:lock\s+(?:that\s+)?(?:in|down)|locked\s+in)\s*[:\-]?\s*([^.;]{10,})", re.IGNORECASE),
    re.compile(r"(?:we\s+)?(?:all\s+)?agreed[,\s]+([^.;]{10,})", re.IGNORECASE),
    re.compile(r"(?:it'?s\s+)?(?:confirmed|done|settled|finalized)[,\s:]+([^.;]{10,})", re.IGNORECASE),
    re.compile(r"(?:we\s+)?(?:will|are\s+going\s+to)\s+(?:decommission|reallocate|freeze|migrate|deploy|move|cut|reduce|shut\s+down)\s+([^.;]{10,})", re.IGNORECASE),
]
# Prefer longer decision phrase when we have multiple from same sentence
_MIN_DECISION_LEN = 10

_QUESTION_PATTERNS = [
    re.compile(r"\?\s*([^?]+)$"),
    re.compile(r"(?:open\s+)?question\s*[:\-]\s*([^.;\n]+)", re.IGNORECASE),
    re.compile(r"(?:we\s+)?(?:still\s+)?(?:need\s+to\s+)?(?:figure\s+out|resolve|decide)\s+[:\s]*([^.;]+)", re.IGNORECASE),
]

_RISK_PATTERNS = [
    re.compile(r"risk\s*[:\-]\s*([^.;\n]+)", re.IGNORECASE),
    re.compile(r"(?:potential\s+)?(?:concern|risk)s?\s*[:\-]?\s*([^.;\n]+)", re.IGNORECASE),
    re.compile(r"(?:we\s+)?(?:are\s+)?(?:worried|concerned)\s+(?:about\s+)?[:\s]*([^.;]+)", re.IGNORECASE),
]

_METRIC_PATTERNS = [
    re.compile(r"(?:target|goal|metric)\s*[:\-]?\s*([^.;\n]+)", re.IGNORECASE),
    re.compile(r"(\d+(?:\.\d+)?\s*%|\d+\s*(?:users?|customers?|revenue|K|M)\b)", re.IGNORECASE),
]

_ASSUMPTION_PATTERNS = [
    re.compile(r"assuming\s+([^.;]+)", re.IGNORECASE),
    re.compile(r"(?:our\s+)?assumption\s*[:\-]\s*([^.;\n]+)", re.IGNORECASE),
]


def _is_valid_owner(owner: str) -> bool:
    if not owner or len(owner) > 40:
        return False
    first = owner.strip().split()[0].lower() if owner.strip() else ""
    return first not in _OWNER_STOPWORDS and not first.isdigit()


def _extract_action_items(
    text: str,
    speaker_id: str,
    chunk_ts: float,
    chunk_id: int,
    known_participants: Optional[set[str]] = None,
) -> list[ActionItemRecord]:
    items: list[ActionItemRecord] = []
    seen: set[str] = set()
    participants = known_participants or set()
    participants = participants | {speaker_id}
    # Loop-invariant: depends only on `participants`, which is fixed for this call.
    known_first = {p.split()[0].lower() for p in participants if p and p.lower() != "unknown"}
    deadline_match = _DEADLINE_PATTERN.search(text)
    default_deadline = deadline_match.group(1).strip() if deadline_match else None
    for line in re.split(r"[.;]\s*", text):
        line = line.strip()
        if len(line) < 10:
            continue
        if _is_open_question_sentence(line):
            continue
        for pat in _ACTION_PATTERNS:
            for m in pat.finditer(line):
                g = m.groups()
                if pat == _ACTION_PATTERNS[0]:
                    owner, task, deadline = (g[0] or "").strip(), (g[1] or "").strip(), (g[2] or "").strip() or default_deadline
                elif pat == _ACTION_PATTERNS[2]:  # "I have X due by Y" — owner = speaker
                    owner, task = speaker_id, (g[0] or "").strip()
                    deadline = (g[1] or "").strip() or default_deadline
                elif pat == _ACTION_PATTERNS[3]:  # "X is due on Y" — no named owner, assign speaker
                    raw_task = (g[0] or "").strip()
                    # Do not double-count "I have … due …" (handled by pattern [2])
                    if re.match(r"^I\s+have\s", raw_task, re.IGNORECASE):
                        continue
                    owner, task = speaker_id, raw_task
                    deadline = (g[1] or "").strip() or default_deadline
                elif pat == _ACTION_PATTERNS[5]:  # "I'll / I will …" — owner = speaker
                    owner, task = speaker_id, (g[0] or "").strip()
                    deadline = (g[1] or "").strip() if len(g) > 1 else default_deadline
                elif pat == _ACTION_PATTERNS[10]:  # "task assigned to X" — g[0]=task, g[1]=owner (reversed)
                    task, owner = (g[0] or "").strip(), (g[1] or "").strip()
                    deadline = default_deadline
                elif pat == _ACTION_PATTERNS[13]:  # "that's on X to Y" — g[0]=owner, g[1]=task (optional)
                    owner = (g[0] or "").strip()
                    task = (g[1] or "").strip() if len(g) > 1 and g[1] else "handle this"
                    deadline = default_deadline
                elif pat == _ACTION_PATTERNS[16]:  # "I'm going to / I plan to …" — owner = speaker
                    owner, task = speaker_id, (g[0] or "").strip()
                    deadline = default_deadline
                elif len(g) >= 2:
                    owner, task = (g[0] or "").strip(), (g[1] or "").strip()
                    deadline = (g[2] or "").strip() if len(g) > 2 else default_deadline
                else:
                    continue
                if not task or len(task) < 8:
                    continue
                # Some patterns produce noun-phrase tasks (assignments, due-dates) —
                # skip the verb-phrase check so "the backend integration" isn't rejected.
                _assignment_style = pat in (
                    _ACTION_PATTERNS[2],   # "I have X due by Y"
                    _ACTION_PATTERNS[3],   # "X is due on Y"
                    _ACTION_PATTERNS[9],   # "X has been assigned Y"
                    _ACTION_PATTERNS[10],  # "task assigned to X"
                    _ACTION_PATTERNS[13],  # "that's on X to Y"
                    _ACTION_PATTERNS[15],  # "X is taking care of Y"
                    _ACTION_PATTERNS[16],  # "I'm going to Y"
                )
                if not _assignment_style and not _is_verb_phrase(task):
                    continue
                if not _is_valid_owner(owner):
                    owner = speaker_id
                # Only reassign to current speaker if we have real participant names and owner isn't one
                owner_canon = owner.split()[0] if owner else ""
                # Only fall back to speaker if we have known participants AND owner isn't one
                # Don't override if owner looks like a real name (capitalized, not a stop word)
                if known_first and owner_canon.lower() not in known_first:
                    # Keep the extracted name if it looks like a proper name
                    if not owner or not owner[0].isupper():
                        owner = speaker_id
                key = f"{owner}:{task[:60]}"
                if key in seen:
                    continue
                seen.add(key)
                items.append(
                    ActionItemRecord(
                        owner=owner,
                        task=task[:200],
                        deadline=deadline or default_deadline,
                        origin_ts=chunk_ts,
                        chunk_id=chunk_id,
                        confidence="high" if deadline and _is_valid_owner(owner) else "medium" if _is_valid_owner(owner) else "low",
                    )
                )
    return items


def _extract_decisions(text: str, chunk_ts: float, chunk_id: int) -> list[DecisionRecord]:
    decisions: list[DecisionRecord] = []
    candidates: list[str] = []
    for line in re.split(r"[.;]\s*", text):
        line = line.strip()
        for pat in _DECISION_PATTERNS:
            for m in pat.finditer(line):
                d = m.group(1).strip()
                if len(d) >= _MIN_DECISION_LEN:
                    candidates.append(d)
    if not candidates:
        return []
    # Dedupe: keep longest; drop if this is a substring of one already kept
    candidates.sort(key=len, reverse=True)
    kept: list[str] = []
    for d in candidates:
        if any(d in other for other in kept):
            continue
        kept.append(d)
        decisions.append(
            DecisionRecord(decision=d, origin_ts=chunk_ts, chunk_id=chunk_id, confidence="high")
        )
    return decisions


def _extract_open_questions(text: str) -> list[str]:
    questions: list[str] = []
    for pat in _QUESTION_PATTERNS:
        for m in pat.finditer(text):
            q = m.group(1).strip()
            if 10 < len(q) < 200 and not q.startswith("Open question:"):
                questions.append(q)
    if "?" in text:
        for part in text.split("?"):
            part = part.strip()
            if 10 < len(part) < 200:
                questions.append(part)
    return list(dict.fromkeys(questions))


def _issue_topic_key(question_text: str) -> str:
    """Normalize question to a topic_key. Works for any meeting type."""
    t = question_text.lower()
    # Common meeting topics — general enough to work across meeting types
    if any(x in t for x in ["budget", "cost", "spend", "overrun", "financial", "dollar", "funding"]):
        return "budget"
    if any(x in t for x in ["deadline", "timeline", "date", "when", "schedule", "milestone", "go-live"]):
        return "timeline"
    if any(x in t for x in ["risk", "concern", "blocker", "issue", "problem", "dependency"]):
        return "risk"
    if any(x in t for x in ["owner", "who owns", "responsible", "assigned", "who is"]):
        return "ownership"
    if any(x in t for x in ["vendor", "supplier", "sdk", "third party", "escalate"]):
        return "vendor"
    if any(x in t for x in ["sprint", "velocity", "story", "point", "backlog", "capacity"]):
        return "sprint"
    if any(x in t for x in ["deploy", "release", "launch", "production", "staging", "freeze"]):
        return "deployment"
    if any(x in t for x in ["feature", "module", "product", "scope", "requirement"]):
        return "product"
    if any(x in t for x in ["engineer", "developer", "team", "resource", "headcount", "hire"]):
        return "team"
    if any(x in t for x in ["api", "endpoint", "integration", "sdk", "version"]):
        return "api"
    if any(x in t for x in ["pricing", "price", "tier", "revenue", "fee"]):
        return "pricing"
    if any(x in t for x in ["format", "round robin", "bracket", "elimination"]):
        return "format"
    if any(x in t for x in ["marketing", "campaign", "announcement", "materials"]):
        return "marketing"
    if any(x in t for x in ["legal", "contract", "agreement", "compliance", "audit"]):
        return "legal"
    # Fallback: slug from first meaningful words
    words = re.findall(r"\b[a-z]{3,}\b", t)
    stop = {"what", "about", "when", "can", "you", "we", "the", "that", "this", "have", "need", "still", "open", "question", "are", "was", "did", "does"}
    words = [w for w in words if w not in stop]
    return "_".join(words[:2]) if words else "other"


def _issue_display_summary(topic_key: str, question_text: str) -> str:
    """Human-readable summary for answers (no raw question text)."""
    t = question_text.lower()
    if topic_key == "format":
        return "tournament format"
    if topic_key == "marketing_deadline":
        return "marketing materials deadline"
    if topic_key == "livestream":
        return "livestream decision"
    if topic_key == "referees":
        if "four" in t or "certified" in t or "ref" in t:
            return "securing four certified referees"
        return "referees"
    if topic_key == "medical_staff":
        return "medical staff onsite"
    return topic_key.replace("_", " ")


def _extract_risks(text: str) -> list[str]:
    risks: list[str] = []
    for pat in _RISK_PATTERNS:
        for m in pat.finditer(text):
            r = m.group(1).strip()
            if len(r) > 5:
                risks.append(r)
    return list(dict.fromkeys(risks))


def _extract_metrics(text: str) -> list[str]:
    metrics: list[str] = []
    for pat in _METRIC_PATTERNS:
        for m in pat.finditer(text):
            metrics.append(m.group(1).strip())
    return list(dict.fromkeys(metrics))


def _extract_assumptions(text: str) -> list[str]:
    assumptions: list[str] = []
    for pat in _ASSUMPTION_PATTERNS:
        for m in pat.finditer(text):
            assumptions.append(m.group(1).strip())
    return list(dict.fromkeys(assumptions))


def _normalize_decision_id(decision_text: str) -> str:
    """Map decision text to a stable id. General enough to work for any meeting type."""
    t = decision_text.lower()
    if any(k in t for k in ["api", "endpoint", "v1", "v2", "version", "sdk"]):
        return "api_strategy"
    if any(k in t for k in ["pricing", "tier", "price", "fee", "revenue"]):
        return "pricing_tier"
    if any(k in t for k in ["launch", "go-live", "release", "ship", "deploy", "production"]):
        return "launch_plan"
    if any(k in t for k in ["deadline", "date", "timeline", "freeze", "milestone"]):
        return "timeline"
    if any(k in t for k in ["budget", "spend", "allocation", "cost", "overrun", "savings"]):
        return "budget"
    if any(k in t for k in ["hiring", "role", "headcount", "recruit"]):
        return "hiring"
    if any(k in t for k in ["cloud", "infrastructure", "environment", "load test", "decommission", "server"]):
        return "infrastructure"
    if any(k in t for k in ["sprint", "velocity", "story point", "capacity", "backlog"]):
        return "sprint_plan"
    if any(k in t for k in ["vendor", "escalate", "sdk", "supplier", "agreement", "contract"]):
        return "vendor"
    if any(k in t for k in ["engineer", "developer", "team", "resource", "reallocate", "reassign"]):
        return "team_allocation"
    if any(k in t for k in ["code freeze", "freeze", "regression", "qa", "testing", "sign-off"]):
        return "qa_process"
    if any(k in t for k in ["feature", "module", "product", "requirement", "scope"]):
        return "product_scope"
    # Fallback: first two meaningful words from the decision text
    words = re.findall(r"\b[a-z]{3,}\b", t)
    words = [w for w in words if w not in {"the", "and", "for", "with", "have", "decided", "agreed", "decision", "that", "this", "will", "going"}]
    return "_".join(words[:2]) if words else "decision_other"


def _classify_topic(text: str) -> str:
    """Classify statement into one topic namespace."""
    t = text.lower()
    best = "general"
    best_count = 0
    for topic, keywords in _TOPIC_KEYWORDS.items():
        count = sum(1 for k in keywords if k in t)
        if count > best_count:
            best_count = count
            best = topic
    return best


def _extract_numeric_claims(text: str, chunk_id: int, speaker_id: str) -> list[tuple[str, str, int, str]]:
    """Extract (concept, value, chunk_id, speaker) for conflict detection. E.g. ('budget', '50K', 1, 'Alice')."""
    out: list[tuple[str, str, int, str]] = []
    t = text.lower()
    # Budget-like numbers
    for m in re.finditer(r"(?:budget|spend|allocation|cost)\s*(?:is|of|:)?\s*(\d+(?:\.\d+)?\s*[KkMm]?)", t, re.IGNORECASE):
        out.append(("budget", m.group(1).strip(), chunk_id, speaker_id))
    for m in re.finditer(r"(\d+(?:\.\d+)?\s*[KkMm])\s*(?:users?|dollars?|budget)", t, re.IGNORECASE):
        out.append(("budget", m.group(1).strip(), chunk_id, speaker_id))
    # Standalone 50K, 40K near "budget" or "users"
    if "budget" in t or "users" in t:
        for m in re.finditer(r"\b(\d+(?:\.\d+)?\s*[KkMm])\b", text):
            out.append(("budget", m.group(1).strip(), chunk_id, speaker_id))
    return out


def _is_verb_phrase(task: str) -> bool:
    """Require task to look like a verb phrase (action), not a open question."""
    action_verbs = {
        "finalize", "send", "own", "schedule", "complete", "review", "draft", "prepare",
        "coordinate", "follow", "update", "confirm", "submit", "get", "reach", "align",
        "book", "design", "contact", "outreach", "reach out",
        "take", "put", "bring", "do", "make", "fix", "call", "check", "move",
        "handle", "lead", "finish", "present", "share", "write", "build", "run",
        "has", "have", "deliver", "push", "launch", "test", "deploy", "research",
        "due", "assigned", "responsible", "working",
    }
    first = task.strip().split()[0].lower() if task.strip() else ""
    return first in action_verbs or task.strip().lower().startswith((
        "will ", "to ", "has to ", "needs to ", "need to ", "due ", "has ",
    ))


def _is_open_question_sentence(sentence: str) -> bool:
    """Do not treat 'we need to figure out X' as an action item."""
    s = sentence.lower()
    if re.search(r"(?:need to|still need to|have to)\s+figure out", s):
        return True
    if re.search(r"open\s+question\s*[:\-]", s):
        return True
    if re.search(r"(?:we\s+)?(?:still\s+)?(?:need to|figure out|resolve|decide)\s+", s) and "?" in sentence:
        return True
    return False


def _canonical_numeric_value(value: str) -> str:
    """Normalize numeric value for conflict detection: 50K == 50k == 50,000 (semantic same)."""
    v = re.sub(r"\s+", "", value.strip().lower())
    # Extract number + optional k/m suffix
    m = re.match(r"^([\d,\.]+)\s*([km])?$", v)
    if m:
        num_part = m.group(1).replace(",", "")
        suffix = (m.group(2) or "").lower()
        try:
            n = float(num_part)
            if suffix == "k":
                n = n * 1000
            elif suffix == "m":
                n = n * 1_000_000
            return str(int(n)) if n == int(n) else str(n)
        except ValueError:
            pass
    return v[:50]


def _owner_canon(owner: str) -> str:
    """Canonical owner for deduplication (first name, lowercase)."""
    return (owner.strip().split()[0] if owner.strip() else "").lower()


def _task_root(task: str) -> str:
    """Normalized task root for deduplication (strip deadline phrasing, lowercase, collapse spaces)."""
    t = task.strip().lower()
    t = re.sub(r"\s+(?:by|due|before|on)\s+[^.;]+$", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:80] if t else ""


def _open_question_resolved_by_action(question: str, owner: str, task: str) -> bool:
    """True if this open question is answered by the ownership action (e.g. who owns X -> X assigned)."""
    q = question.lower()
    t = task.lower()
    if "own" not in t:
        return False
    # "who owns the marketing rollout?" + "Carol will own the marketing rollout" -> resolved
    if "who" in q and "own" in q:
        # Check overlap: significant words from question appear in task
        q_words = {w for w in re.findall(r"\b[a-z]{3,}\b", q) if w not in {"who", "owns", "the", "for", "about"}}
        t_words = set(re.findall(r"\b[a-z]{3,}\b", t))
        if q_words & t_words:
            return True
    return False


# --- Issue lifecycle: resolution / postpone (deterministic rules) ---

def _chunk_resolves_format(engine: "ContextIntegrityEngine") -> bool:
    """True if current decisions include format (single elimination, etc.)."""
    for _did, val in engine.get_current_decisions():
        v = val.lower()
        if "single elimination" in v or "round robin" in v or "format" in _did.lower() or "elimination" in v or "bracket" in v:
            return True
    return False


def _chunk_has_concrete_deadline_answer(text: str) -> bool:
    """True if text looks like an answer with a concrete date (resolves marketing_deadline etc.)."""
    t = text.lower()
    if re.search(r"\bby\s+(?:next\s+)?(?:this\s+)?(?:wednesday|friday|monday|tuesday|thursday|saturday|sunday)\b", t):
        return True
    if re.search(r"\bby\s+\d{1,2}/\d{1,2}", t) or re.search(r"\b(?:end of|next)\s+\w+\b", t):
        return True
    return False


def _chunk_postpones_topic(text: str, topic_key: str) -> bool:
    """True if text explicitly postpones/tables the given topic (chunk must mention the topic)."""
    t = text.lower()
    if not any(p in t for p in ["postpone", "table that", "table it", "we'll decide later", "decide later", "defer", "put off"]):
        return False
    if topic_key == "livestream":
        return "livestream" in t or "stream" in t or "finals" in t
    # Only livestream is explicitly matched above; do not postpone other topics by default
    return False


def extract_from_chunk(
    text: str,
    speaker_id: str,
    chunk_id: int,
    participants: Optional[set[str]] = None,
) -> tuple[list[tuple[str, str, Optional[str]]], list[tuple[str, str]], list[str]]:
    """
    Extraction from a single chunk of text (no engine state).
    Returns (action_items, decisions, open_questions) for use by retrieval/callers.
    event_ids belong to the retrieval layer; caller sets event_ids=[chunk_id].
    """
    parts = participants or set()
    parts.add(speaker_id)
    items = _extract_action_items(text, speaker_id, 0.0, chunk_id, parts)
    decs = _extract_decisions(text, 0.0, chunk_id)
    open_q = _extract_open_questions(text)
    action_tuples = [(ai.owner, ai.task, ai.deadline) for ai in items]
    decision_tuples = [(_normalize_decision_id(d.decision), d.decision) for d in decs]
    return (action_tuples, decision_tuples, open_q)


def _token_overlap(later_text: str, decision_text: str) -> float:
    """Fraction of decision tokens that appear in later text (for reversal detection)."""
    t_later = set(re.findall(r"\b\w+\b", later_text.lower()))
    t_decision = set(re.findall(r"\b\w+\b", decision_text.lower()))
    if not t_decision:
        return 0.0
    return len(t_later & t_decision) / len(t_decision)


def _detect_negation_reversal(later: str, earlier: str) -> bool:
    """Heuristic: later statement reverses earlier if same topic + negation."""
    neg = re.compile(
        r"\b(no(?:t)?|never|won't|wouldn't|shouldn't|cancel|revert|reverse|instead|actually\s+we|actually\s+ignore)\b",
        re.IGNORECASE,
    )
    if not neg.search(later):
        return False
    overlap = _token_overlap(later, earlier)
    # Lower threshold when "actually" present (e.g. "actually ignore what I said" + "old API")
    if "actually" in later.lower() and overlap >= 0.25:
        return True
    return overlap >= 0.5


def _extract_reversal_value(reversal_text: str, old_value: str) -> Optional[str]:
    """Extract the new decision value from a reversal sentence (e.g. 'revert to v1' -> 'v1')."""
    t = reversal_text.lower()
    # "revert to X", "go back to X", "stay on X", "use X", "let's go with X"
    for pat in [
        r"revert\s+to\s+([^.;,]+?)(?:\.|;|,|$)",
        r"go\s+back\s+to\s+([^.;,]+?)(?:\.|;|,|$)",
        r"stay\s+on\s+([^.;,]+?)(?:\.|;|,|$)",
        r"(?:let'?s\s+)?(?:go\s+)?with\s+([^.;,]+?)(?:\.|;|,|$)",
        r"use\s+(?:the\s+)?([^.;,]+?)(?:\.|;|,|$)",
        r"final\s+decision\s+is\s+([^.;,]+?)(?:\.|;|,|$)",
    ]:
        m = re.search(pat, t, re.IGNORECASE)
        if m:
            return m.group(1).strip()[:120]
    # Fallback: "stay on X" / "old API" / "let's X" as phrase (no trailing .;,$ required)
    m = re.search(r"stay\s+on\s+(?:the\s+)?([^.;]+?)(?:\s+until|\s*\.|;|$)", t, re.IGNORECASE)
    if m:
        return m.group(1).strip()[:80]
    if "old api" in t or "v1" in t:
        return "old API" if "old api" in t else "v1"
    # Fallback: take a short phrase after "actually we" / "instead"
    m = re.search(r"(?:actually\s+we|instead)\s+[^.;]*?\b(\w+(?:\s+\w+)?)\s*(?:\.|;|$)", t, re.IGNORECASE)
    if m:
        return m.group(1).strip()[:80]
    return None


class ContextIntegrityEngine:
    """
    Processes meeting transcript in chronological chunks.
    Maintains structured memory; no summarization until END OF MEETING.
    Uses stateful decisions, topic partitioning, and conflict graph.
    """

    def __init__(self, meeting_id: str = "default"):
        self.meeting_id = meeting_id
        self._chunk_id = 0
        self._facts: list[FactRecord] = []
        self._decisions: list[DecisionRecord] = []  # kept in sync for backward compat
        self._decision_states: dict[str, DecisionState] = {}  # canonical state
        self._action_items: list[ActionItemRecord] = []
        self._risks: list[tuple[str, float, int]] = []
        self._metrics: list[tuple[str, float, int]] = []
        self._assumptions: list[tuple[str, float, int]] = []
        self._speaker_positions: list[tuple[str, str, str, float, int]] = []
        self._chunk_timestamps: list[float] = []
        self._raw_chunks: list[tuple[float, str, str]] = []
        self._known_participants: set[str] = set()
        self._conflicts: list[ConflictRecord] = []
        self._reversal_log: list[tuple[str, int, str, int]] = []  # (decision_id, from_chunk, new_value, to_chunk)
        self._resolved_open_question_keys: set[str] = set()  # legacy; used only for action-ownership resolution
        self._issues: list[Issue] = []  # Issue lifecycle: open | resolved | postponed
        self._tagged_chunks: list[TaggedChunk] = []  # chunk_id, text, speaker_id, topic, tags (for retrieval)
        self._embedding_by_chunk_id: dict[int, list[float]] = {}
        self._query_embedding_cache: OrderedDict[str, list[float]] = OrderedDict()

    def register_participants(self, names: list[str] | tuple[str, ...] | set[str]) -> int:
        """Register call participants from platform roster metadata.

        FaceTime has one mixed audio stream, so diarization can separate voices
        but cannot reliably name them. The roster still gives Mechus important
        context for "who's on the call?" and action-item ownership. Returns the
        number of newly-added participant names.
        """
        existing = {p.lower() for p in self._known_participants}
        added = 0
        for raw in names or []:
            name = str(raw or "").strip()
            if not name:
                continue
            key = name.lower()
            if key in existing:
                continue
            existing.add(key)
            self._known_participants.add(name)
            self._facts.append(
                FactRecord(
                    fact=f"[system] Participant in this meeting: {name}",
                    speaker_id="system",
                    origin_ts=time.time(),
                    chunk_id=self._chunk_id,
                    confidence="high",
                    topic="general",
                )
            )
            added += 1
        return added

    def _query_embed_cache_key(self, request_text: str) -> str:
        t = " ".join((request_text or "").lower().split())
        return hashlib.sha256(t.encode("utf-8")).hexdigest()[:32]

    def _get_query_embedding_cached(self, key: str) -> list[float] | None:
        if key in self._query_embedding_cache:
            self._query_embedding_cache.move_to_end(key)
            return self._query_embedding_cache[key]
        return None

    def _put_query_embedding_cached(self, key: str, vec: list[float], max_entries: int) -> None:
        if key in self._query_embedding_cache:
            del self._query_embedding_cache[key]
        self._query_embedding_cache[key] = vec
        while len(self._query_embedding_cache) > max_entries:
            self._query_embedding_cache.popitem(last=False)

    def _apply_issue_resolution(self, chunk_text: str, chunk_id: int) -> None:
        """Apply resolution/postpone rules to open issues (deterministic)."""
        for issue in self._issues:
            if issue.status != ISSUE_STATUS_OPEN:
                continue
            # B) Resolve when decision mentions same topic_key (e.g. format)
            if issue.topic_key == "format" and _chunk_resolves_format(self):
                issue.status = ISSUE_STATUS_RESOLVED
                issue.resolved_chunk_id = chunk_id
                issue.evidence_chunk_ids = list(set(issue.evidence_chunk_ids + [chunk_id]))
                continue
            # A) Resolve when concrete date answer (e.g. "By next Wednesday" -> marketing_deadline)
            if issue.topic_key == "marketing_deadline" and _chunk_has_concrete_deadline_answer(chunk_text):
                issue.status = ISSUE_STATUS_RESOLVED
                issue.resolved_chunk_id = chunk_id
                issue.evidence_chunk_ids = list(set(issue.evidence_chunk_ids + [chunk_id]))
                continue
            # C) Postpone when language indicates deferral
            if _chunk_postpones_topic(chunk_text, issue.topic_key):
                issue.status = ISSUE_STATUS_POSTPONED
                issue.resolved_chunk_id = chunk_id
                issue.evidence_chunk_ids = list(set(issue.evidence_chunk_ids + [chunk_id]))
                continue

    def ingest_chunk(
        self,
        text: str,
        *,
        speaker_id: str = "unknown",
        timestamp: Optional[float] = None,
    ) -> tuple[MemoryUpdate, bool, Optional[TaggedChunk]]:
        """
        Process one transcript chunk. Returns (MemoryUpdate, is_end_of_meeting, tagged_chunk).
        tagged_chunk is set when the chunk was fully processed and added for retrieval; None for
        end-of-meeting trigger, very short text, or empty input.
        If text is the end-of-meeting trigger, is_end_of_meeting=True and you should call generate_final_report() next.
        """
        if timestamp is None:
            timestamp = time.time()
        self._chunk_id += 1
        cid = self._chunk_id
        self._chunk_timestamps.append(timestamp)
        self._raw_chunks.append((timestamp, speaker_id, text))

        if _is_end_of_meeting(text):
            return (
                MemoryUpdate(
                    new_facts=[],
                    updated_facts=[],
                    conflicts_detected=[],
                    action_items_added=[],
                    decisions_updated=[],
                    open_loops_remaining=self._format_open_loops(),
                ),
                True,
                None,
            )

        new_facts: list[str] = []
        updated_facts: list[str] = []
        conflicts: list[str] = []
        action_added: list[str] = []
        decisions_updated: list[str] = []

        text_norm = re.sub(r"\s+", " ", text).strip()
        if len(text_norm) < 3:
            return (
                MemoryUpdate(
                    new_facts=[],
                    updated_facts=[],
                    conflicts_detected=[],
                    action_items_added=[],
                    decisions_updated=[],
                    open_loops_remaining=self._format_open_loops(),
                ),
                False,
                None,
            )

        self._known_participants.add(speaker_id)
        topic = _classify_topic(text_norm)
        fact_candidate = f"[{speaker_id}] {text_norm[:500]}"
        self._facts.append(
            FactRecord(
                fact=fact_candidate,
                speaker_id=speaker_id,
                origin_ts=timestamp,
                chunk_id=cid,
                confidence="low",
                topic=topic,
            )
        )
        new_facts.append(fact_candidate[:200])

        # 1) Reversal handling: check stateful decisions first; update state and legacy _decisions
        for did, state in list(self._decision_states.items()):
            if state.reversed and state.last_updated_chunk == cid:
                continue
            if _detect_negation_reversal(text_norm, state.current_value):
                new_val = _extract_reversal_value(text_norm, state.current_value)
                if not new_val or "(reversed in chunk" in (new_val or ""):
                    t = text_norm.lower()
                    if "old api" in t:
                        new_val = "old API"
                    elif "v1" in t:
                        new_val = "v1"
                    elif "stay on" in t:
                        m = re.search(r"stay\s+on\s+(?:the\s+)?(\w+(?:\s+\w+)?)", t)
                        new_val = m.group(1).strip() if m else None
                new_val = new_val or f"(reversed in chunk {cid})"
                self._reversal_log.append((did, state.last_updated_chunk, new_val, cid))
                prev_val = state.current_value
                state.history.append((state.last_updated_chunk, state.current_value))
                state.current_value = new_val
                state.reversed = True
                state.last_updated_chunk = cid
                state.owner = speaker_id
                conflicts.append(f"Decision reversed: \"{prev_val[:80]}...\" (reversed in chunk {cid})")
                decisions_updated.append(f"REVERSED: {prev_val[:100]}")
                # Mark legacy record reversed; add new record for current value
                for i, d in enumerate(self._decisions):
                    if d.reversed_by_chunk_id is None and _token_overlap(d.decision, prev_val) >= 0.5:
                        self._decisions[i] = DecisionRecord(
                            decision=d.decision,
                            origin_ts=d.origin_ts,
                            chunk_id=d.chunk_id,
                            reversed_by_chunk_id=cid,
                            confidence=d.confidence,
                        )
                        break
                self._decisions.append(
                    DecisionRecord(decision=new_val, origin_ts=timestamp, chunk_id=cid, confidence="high")
                )

        # 2) Conflict graph: canonical numeric comparison (50K == 50k, no fake conflicts)
        for concept, value, _cid, spk in _extract_numeric_claims(text_norm, cid, speaker_id):
            canonical = _canonical_numeric_value(value)
            existing = next((c for c in self._conflicts if c.name == concept), None)
            if existing:
                vals = existing.values_mentioned
                if not any(_canonical_numeric_value(v[0]) == canonical for v in vals):
                    vals.append((value, _cid, spk))
                    existing.status = "unresolved"
                    conflicts.append(
                        f"Conflict ({concept}): mentioned {existing.values_mentioned[0][0]} and {value} (unresolved)"
                    )
            else:
                self._conflicts.append(
                    ConflictRecord(
                        name=concept,
                        values_mentioned=[(value, _cid, spk)],
                        status="unresolved",
                    )
                )

        # 3) Action items: resolve pronouns using recent context before extracting
        def _resolve_pronouns(chunk_text: str) -> str:
            """Replace vague pronouns with recent topic if context is available."""
            vague = re.compile(r"\b(that|this|it|those|these)\b", re.IGNORECASE)
            if not vague.search(chunk_text):
                return chunk_text
            # Get last 3 chunks as context
            recent = self._raw_chunks[-4:-1] if len(self._raw_chunks) > 1 else []
            if not recent:
                return chunk_text
            recent_text = " ".join(r[2] for r in recent).lower()
            # Find the most recently mentioned noun phrase as the topic
            topic_match = re.search(
                r"\b([\w\s]{3,30}(?:project|task|report|presentation|document|plan|proposal|design|feature|module|integration|model|system|app|site|page|form|script|code|file|test|review|draft|email|meeting|call|demo|pitch|deck|slide|survey|budget|contract|invoice|order|ticket|issue|bug|fix|update|release|deploy|launch|campaign|post|article|blog|video|event|workshop|training|class|course|assignment|homework|quiz|exam|thesis|paper|essay|submission))\b",
                recent_text
            )
            if topic_match:
                topic_phrase = topic_match.group(1).strip()
                chunk_text = vague.sub(topic_phrase, chunk_text)
            return chunk_text

        resolved_text = _resolve_pronouns(text_norm)
        existing_roots = {(_owner_canon(ai.owner), _task_root(ai.task)) for ai in self._action_items}
        for ai in _extract_action_items(resolved_text, speaker_id, timestamp, cid, self._known_participants):
            tr = _task_root(ai.task)
            if not tr:
                continue
            key = (_owner_canon(ai.owner), tr)
            if key in existing_roots:
                continue
            existing_roots.add(key)
            self._action_items.append(ai)
            action_added.append(f"{ai.owner}: {ai.task[:120]}")

        # 4) New decisions: merge into stateful + legacy
        for d in _extract_decisions(text, timestamp, cid):
            did = _normalize_decision_id(d.decision)
            if did in self._decision_states and self._decision_states[did].last_updated_chunk == cid:
                continue  # already updated by reversal this chunk
            if did not in self._decision_states:
                self._decision_states[did] = DecisionState(
                    id=did,
                    current_value=d.decision,
                    history=[],
                    reversed=False,
                    owner=speaker_id,
                    last_updated_chunk=cid,
                    origin_ts=timestamp,
                    confidence=d.confidence,
                )
                self._decisions.append(d)
                decisions_updated.append(d.decision)
            else:
                # Reaffirmation or update
                self._decision_states[did].current_value = d.decision
                self._decision_states[did].last_updated_chunk = cid
                self._decisions.append(d)
                decisions_updated.append(d.decision)

        for q in _extract_open_questions(text):
            topic_key = _issue_topic_key(q)
            if any(i.topic_key == topic_key and i.status == ISSUE_STATUS_OPEN for i in self._issues):
                continue  # one open issue per topic_key
            display = _issue_display_summary(topic_key, q)
            issue_id = f"issue_{cid}_{topic_key}_{len(self._issues)}"
            self._issues.append(
                Issue(
                    issue_id=issue_id,
                    topic_key=topic_key,
                    text=q,
                    status=ISSUE_STATUS_OPEN,
                    opened_chunk_id=cid,
                    evidence_chunk_ids=[cid],
                    display_summary=display,
                )
            )
        self._apply_issue_resolution(text_norm, cid)

        for r in _extract_risks(text):
            self._risks.append((r, timestamp, cid))

        for m in _extract_metrics(text):
            self._metrics.append((m, timestamp, cid))

        for a in _extract_assumptions(text):
            self._assumptions.append((a, timestamp, cid))

        self._speaker_positions.append((speaker_id, "statement", text_norm[:300], timestamp, cid))

        # Tag this chunk for retrieval (topic + type so we send only relevant chunks to the LLM)
        tags: list[str] = []
        if action_added:
            tags.append("action_item")
        if decisions_updated:
            tags.append("decision")
        for i in self._issues:
            if i.opened_chunk_id == cid:
                tags.append("open_question")
                break
        tagged = TaggedChunk(chunk_id=cid, text=text_norm, speaker_id=speaker_id, topic=topic, tags=tags)
        self._tagged_chunks.append(tagged)

        return (
            MemoryUpdate(
                new_facts=new_facts,
                updated_facts=updated_facts,
                conflicts_detected=conflicts,
                action_items_added=action_added,
                decisions_updated=decisions_updated,
                open_loops_remaining=self._format_open_loops(),
            ),
            False,
            tagged,
        )

    def _format_open_loops(self) -> list[str]:
        out: list[str] = []
        for i in self._issues:
            if i.status != ISSUE_STATUS_OPEN:
                continue
            out.append(f"[Chunk {i.opened_chunk_id}] Open: {i.display_summary or i.text[:80]}")
        for ai in self._action_items:
            if ai.deadline is None:
                out.append(f"Action (no deadline): {ai.owner} - {ai.task[:80]}")
        return out[:15]

    def get_action_items_structured(self) -> list[tuple[str, str, Optional[str]]]:
        """Return [(owner, task, deadline)] for deterministic retrieval (no LLM improv)."""
        return [(ai.owner, ai.task, ai.deadline) for ai in self._action_items]

    def merge_extracted_action_items(
        self,
        items: list[dict],
        *,
        chunk_id: int,
        ts: float,
    ) -> int:
        """Merge LLM-extracted action items into canonical state with dedup.

        The regex extractor in ``ingest_chunk`` only catches a handful of
        templated phrasings ("X will Y", "I'll Y", "due by Z"). Long meetings
        are full of paraphrased commitments ("can you take the deck?", "let's
        have Priya own the rollout") that it misses. This lets the chapter map
        step feed back LLM-extracted items, deduped against existing ones by
        (owner first-name, task root). Returns the number of new items added.
        """
        if not items:
            return 0
        existing_roots = {
            (_owner_canon(ai.owner), _task_root(ai.task)) for ai in self._action_items
        }
        added = 0
        for it in items:
            if not isinstance(it, dict):
                continue
            owner = str(it.get("owner") or "").strip()
            task = str(it.get("task") or "").strip()
            deadline = str(it.get("deadline") or "").strip() or None
            tr = _task_root(task)
            if not task or len(task) < 6 or not tr:
                continue
            if not _is_valid_owner(owner):
                owner = "unassigned"
            key = (_owner_canon(owner), tr)
            if key in existing_roots:
                continue
            existing_roots.add(key)
            self._action_items.append(
                ActionItemRecord(
                    owner=owner,
                    task=task,
                    deadline=deadline,
                    origin_ts=ts,
                    chunk_id=chunk_id,
                    confidence="medium",
                )
            )
            added += 1
        return added

    def get_open_questions_list(self) -> list[str]:
        """Return only issues with status==open; display_summary (no raw question text)."""
        return [
            i.display_summary or i.topic_key.replace("_", " ")
            for i in self._issues
            if i.status == ISSUE_STATUS_OPEN
        ]

    def get_postponed_issues_list(self) -> list[str]:
        """Return display summary for postponed issues (e.g. 'livestream decision')."""
        return [
            (i.display_summary or i.topic_key.replace("_", " ")) + " (postponed)"
            for i in self._issues
            if i.status == ISSUE_STATUS_POSTPONED
        ]

    def get_current_decisions(self) -> list[tuple[str, str]]:
        """Return [(decision_id, current_value)] for QA — only current state, no superseded."""
        return [(s.id, s.current_value) for s in self._decision_states.values()]

    def get_canonical_state_for_qa(self) -> dict:
        """
        Return canonical state for responder/LLM (no transcript dump).
        Decisions, action items, open/postponed issues, reversals — all structured.
        """
        decisions = [{"id": did, "value": val} for did, val in self.get_current_decisions()]
        action_items = [
            {"owner": ai.owner, "task": ai.task, "deadline": ai.deadline}
            for ai in self._action_items
        ]
        open_issues = [
            {
                "topic_key": i.topic_key,
                "status": i.status,
                "display_summary": i.display_summary or i.topic_key.replace("_", " "),
            }
            for i in self._issues
            if i.status in (ISSUE_STATUS_OPEN, ISSUE_STATUS_POSTPONED)
        ]
        reversals = [
            {"decision_id": did, "new_value": val}
            for did, _from, val, _to in self._reversal_log[-10:]
        ]
        return {
            "decisions": decisions,
            "action_items": action_items,
            "open_issues": open_issues,
            "reversals": reversals,
        }

    def latest_chunk_timestamp(self) -> float | None:
        """Most recent ingest_chunk timestamp, or None. Use as meeting_now_ts for time-window recaps."""
        if not self._chunk_timestamps:
            return None
        return max(self._chunk_timestamps)

    def get_qa_event_ids(self) -> list[int]:
        """Chunk ids that contributed to current action items, decisions, and open/postponed issues (for grounding)."""
        ids: list[int] = []
        for ai in self._action_items:
            ids.append(ai.chunk_id)
        for state in self._decision_states.values():
            ids.append(state.last_updated_chunk)
        for i in self._issues:
            if i.status in (ISSUE_STATUS_OPEN, ISSUE_STATUS_POSTPONED):
                ids.append(i.opened_chunk_id)
                if i.resolved_chunk_id is not None:
                    ids.append(i.resolved_chunk_id)
        return list(dict.fromkeys(ids))[:25]

    def get_reversal_log(self) -> list[tuple[str, int, str, int]]:
        """Return [(decision_id, from_chunk, new_value, to_chunk)] for QA."""
        return list(self._reversal_log)

    def get_conflicts(self) -> list[ConflictRecord]:
        """Return structured conflicts (e.g. budget 50K vs 40K) for QA."""
        return [c for c in self._conflicts if len(c.values_mentioned) >= 2]

    def get_facts_for_topic(self, topic: str, max_chars: int = 2000) -> str:
        """Return facts and decisions for a single topic namespace (focused retrieval)."""
        parts: list[str] = []
        for f in self._facts:
            if f.topic == topic or topic == "general":
                parts.append(f.fact)
        for state in self._decision_states.values():
            if _classify_topic(state.current_value) == topic or topic == "general":
                parts.append(f"Decision ({state.id}): {state.current_value}")
        text = " ".join(parts)
        return text[:max_chars] if len(text) > max_chars else text

    def get_relevant_chunks(
        self,
        request_text: str,
        intent: str,
        topic: Optional[str],
        max_chars: int = 4000,
        *,
        retrieval_mode: str = "lexical",
        bm25_weight: float = 0.45,
        tag_weight: float = 0.55,
        embed_weight: float = 0.32,
        embed_rerank_top_k: int = 28,
        openai_api_key: Optional[str] = None,
        embedding_model: str = "text-embedding-3-small",
        retrieval_auto_lexical_gap_min: float = 0.12,
        retrieval_auto_top_floor_hybrid: float = 0.18,
        query_embed_cache_max: int = 128,
        out_meta: Optional[dict] = None,
        recap_use_standard_path: bool = False,
        recency_weight: float = 0.0,
        ensure_recent_tail_chunks: int = 0,
    ) -> tuple[str, list[int], list["TaggedChunk"]]:
        """
        Return only the transcript chunks relevant to the question (tagged by topic + type).
        Lexical: BM25 + tag fusion. Hybrid: embedding rerank on top-K. Auto: lexical-first, embed when weak/semantic.
        Returns (formatted_context_string, event_ids, list of TaggedChunk for fallback).

        When intent is recap_query and recap_use_standard_path is True, use BM25/hybrid scoring
        (topic- or keyword-scoped recap) instead of the chapter + tail shortcut.
        """
        if not self._tagged_chunks:
            return "", [], []
        raw_mode = (retrieval_mode or "lexical").strip().lower()
        if raw_mode not in ("lexical", "hybrid", "auto"):
            raw_mode = "lexical"

        # Full meeting recap: chapters (if any) + chronological tail within budget (most recent first).
        if intent == "recap_query" and not recap_use_standard_path:
            from mechus.meeting_chapters import format_chapters_for_prompt, load_chapters

            ch_text = ""
            try:
                base = os.environ.get("MECHUS_MEETING_STORE_DIR", "mechus_meetings")
                ch = load_chapters(base, self.meeting_id)
                ch_budget = min(10000, max(2000, max_chars // 2))
                ch_text = format_chapters_for_prompt(ch, max_chars=ch_budget)
            except Exception:
                ch_text = ""
            prefix = ""
            if ch_text:
                prefix = "### Rolling chapter summaries\n" + ch_text + "\n\n### Transcript (chronological)\n"
            tail_budget = max_chars - len(prefix) - 8
            if tail_budget < 500:
                tail_budget = max(400, max_chars // 4)
            ordered = sorted(self._tagged_chunks, key=lambda tc: tc.chunk_id)
            parts: list[str] = []
            event_ids: list[int] = []
            chosen_chunks: list[TaggedChunk] = []
            total = 0
            # Prefer the most recent transcript: walk from the end until the budget is used.
            acc: list[tuple[TaggedChunk, str]] = []
            for tc in reversed(ordered):
                line = f"[{tc.speaker_id}] {tc.text}"
                line_len = len(line) + 1
                if total + line_len > tail_budget and acc:
                    break
                if total + line_len > tail_budget and not acc:
                    line = line[: max(0, tail_budget - 25)] + "..."
                    line_len = len(line) + 1
                acc.append((tc, line))
                total += line_len
                if total >= tail_budget:
                    break
            acc.reverse()
            for tc, line in acc:
                parts.append(line)
                event_ids.append(tc.chunk_id)
                chosen_chunks.append(tc)
            body = "\n".join(parts)
            full = prefix + body if prefix else body
            if out_meta is not None:
                out_meta["recap_chapters_chars"] = len(ch_text)
                out_meta["recap_transcript_chars"] = len(body)
                out_meta["retrieval_mode"] = raw_mode
                out_meta["effective_retrieval_mode"] = "lexical"
                out_meta["lexical_confidence"] = None
                out_meta["hybrid_skipped_reason"] = "recap_path"
                out_meta["selected_chunk_ids"] = list(event_ids)
            return full, event_ids, chosen_chunks

        request_l = request_text.lower()
        request_terms = set(re.findall(r"\b[a-z0-9]{3,}\b", request_l))
        request_terms -= {"what", "when", "where", "who", "why", "how", "did", "does", "have", "has", "the", "and", "for", "about", "that", "this", "with"}

        n = len(self._tagged_chunks)
        raw_tag: list[float] = []
        for tc in self._tagged_chunks:
            score = 0.0
            if intent == "action_item_query" and "action_item" in tc.tags:
                score += 10.0
            if intent == "decision_query" and "decision" in tc.tags:
                score += 10.0
            if intent == "open_question_query" and "open_question" in tc.tags:
                score += 10.0
            if topic and (tc.topic == topic or tc.topic == "general"):
                score += 3.0
            if request_terms:
                chunk_l = tc.text.lower()
                hits = sum(1 for t in request_terms if t in chunk_l)
                score += min(hits * 0.5, 5.0)
            raw_tag.append(score)

        docs = [tc.text for tc in self._tagged_chunks]
        bm = bm25_scores(request_text, docs)
        nt = min_max_norm(raw_tag)
        nb = min_max_norm(bm)
        tw = max(0.0, min(1.0, tag_weight))
        bw = max(0.0, min(1.0, bm25_weight))
        ssum = tw + bw
        if ssum <= 0:
            tw, bw = 0.5, 0.5
            ssum = 1.0
        tw, bw = tw / ssum, bw / ssum
        combined = [tw * nt[i] + bw * nb[i] for i in range(n)]

        rw = max(0.0, min(0.5, float(recency_weight)))
        if n > 1 and rw > 0:
            denom = max(n - 1, 1)
            for i in range(n):
                combined[i] += rw * (i / denom)

        full_order = sorted(range(n), key=lambda i: -combined[i])
        k = max(1, min(embed_rerank_top_k, n))
        topk_idx = full_order[:k]
        rest_idx = full_order[k:]

        lexical_gap: float | None = None
        if n >= 2:
            lexical_gap = combined[full_order[0]] - combined[full_order[1]]
        elif n == 1:
            lexical_gap = 1.0

        effective_for_embed = "lexical"
        auto_reason = ""
        if raw_mode == "auto":
            effective_for_embed, gap_auto, auto_reason = _effective_retrieval_for_auto(
                intent,
                request_text,
                combined,
                full_order,
                n,
                gap_min_lexical=retrieval_auto_lexical_gap_min,
                top_floor_hybrid=retrieval_auto_top_floor_hybrid,
            )
            lexical_gap = gap_auto
        elif raw_mode == "hybrid":
            effective_for_embed = "hybrid"

        hybrid_skipped_reason = ""
        if raw_mode == "lexical":
            hybrid_skipped_reason = "explicit_lexical"
        elif raw_mode == "auto" and effective_for_embed == "lexical":
            hybrid_skipped_reason = auto_reason

        ew = max(0.0, min(1.0, embed_weight))
        api_ok = bool(openai_api_key and openai_api_key.strip())
        do_hybrid_embed = bool(effective_for_embed == "hybrid" and ew > 0 and api_ok and topk_idx)

        if raw_mode == "hybrid" and not api_ok:
            hybrid_skipped_reason = hybrid_skipped_reason or "no_openai_key"
        elif raw_mode == "hybrid" and ew <= 0:
            hybrid_skipped_reason = hybrid_skipped_reason or "embed_weight_zero"

        topk_reranked = list(topk_idx)
        embed_rerank_applied = False
        hybrid_fusion_ran = False
        query_cache_hit = False
        chunk_cache_hits = 0

        if do_hybrid_embed:
            q_key = self._query_embed_cache_key(request_text)
            q_vec = self._get_query_embedding_cached(q_key)
            if q_vec is not None:
                query_cache_hit = True
            chunk_vecs: list[list[float] | None] = [None] * len(topk_idx)
            missing_pos: list[int] = []
            for j in range(len(topk_idx)):
                idx = topk_idx[j]
                cid = self._tagged_chunks[idx].chunk_id
                cached_c = self._embedding_by_chunk_id.get(cid)
                if cached_c is not None:
                    chunk_vecs[j] = cached_c
                    chunk_cache_hits += 1
                else:
                    missing_pos.append(j)

            texts: list[str] = []
            if q_vec is None:
                texts.append(request_text)
            for j in missing_pos:
                texts.append(self._tagged_chunks[topk_idx[j]].text)

            emb: list[list[float]] | None = None
            if texts:
                emb = fetch_embeddings(texts, api_key=openai_api_key, model=embedding_model)
            if texts and emb and len(emb) == len(texts):
                off = 0
                if q_vec is None:
                    q_vec = emb[0]
                    off = 1
                    self._put_query_embedding_cached(q_key, q_vec, query_embed_cache_max)
                for mi, j in enumerate(missing_pos):
                    vec = emb[off + mi]
                    cid = self._tagged_chunks[topk_idx[j]].chunk_id
                    self._embedding_by_chunk_id[cid] = vec
                    chunk_vecs[j] = vec
            elif texts:
                hybrid_skipped_reason = hybrid_skipped_reason or "embedding_fetch_failed"

            if q_vec is not None and all(chunk_vecs[jj] is not None for jj in range(len(topk_idx))):
                sims = [cosine_similarity(q_vec, chunk_vecs[jj] or []) for jj in range(len(topk_idx))]
                ns = min_max_norm(sims)
                finals = [(1.0 - ew) * combined[topk_idx[j]] + ew * ns[j] for j in range(len(topk_idx))]
                order_in_top = sorted(range(len(topk_idx)), key=lambda j: -finals[j])
                topk_reranked = [topk_idx[j] for j in order_in_top]
                embed_rerank_applied = topk_reranked != topk_idx
                hybrid_fusion_ran = True
            elif do_hybrid_embed and not hybrid_skipped_reason:
                hybrid_skipped_reason = "embedding_fetch_failed"

        pack_order = topk_reranked + rest_idx
        tail_k = max(0, int(ensure_recent_tail_chunks))
        if tail_k > 0 and n > 0:
            tail_indices = list(range(max(0, n - tail_k), n))
            seen_pack: set[int] = set()
            merged_order: list[int] = []
            for i in tail_indices + pack_order:
                if i in seen_pack:
                    continue
                seen_pack.add(i)
                merged_order.append(i)
            pack_order = merged_order

        parts: list[str] = []
        event_ids: list[int] = []
        chosen_chunks: list[TaggedChunk] = []
        total = 0
        for i in pack_order:
            tc = self._tagged_chunks[i]
            if total >= max_chars:
                break
            line = f"[{tc.speaker_id}] {tc.text}"
            if total + len(line) + 1 > max_chars:
                line = line[: max_chars - total - 20] + "..."
            parts.append(line)
            event_ids.append(tc.chunk_id)
            chosen_chunks.append(tc)
            total += len(line) + 1

        if out_meta is not None:
            out_meta["retrieval_mode"] = raw_mode
            out_meta["effective_retrieval_mode"] = "hybrid" if hybrid_fusion_ran else "lexical"
            out_meta["lexical_confidence"] = lexical_gap
            out_meta["hybrid_skipped_reason"] = hybrid_skipped_reason
            out_meta["bm25_weight"] = bm25_weight
            out_meta["tag_weight"] = tag_weight
            out_meta["embed_weight"] = embed_weight
            out_meta["selected_chunk_ids"] = list(event_ids)
            out_meta["recency_weight"] = rw
            out_meta["ensure_recent_tail_chunks"] = tail_k
            out_meta["embed_rerank_used"] = hybrid_fusion_ran
            out_meta["embed_rerank_reordered"] = embed_rerank_applied
            out_meta["query_embed_cache_hit"] = query_cache_hit
            out_meta["chunk_embed_cache_hits"] = chunk_cache_hits

        return "\n".join(parts), event_ids, chosen_chunks

    def get_state_for_qa(self, topic: Optional[str] = None) -> str:
        """
        Return a short, deterministic state summary for LLM context.
        Use for focused retrieval: pass only this + topic-scoped facts to avoid flooding.
        """
        lines: list[str] = []
        lines.append("Current decisions (canonical state — use these as the final answer; reversals below are for context only):")
        for did, val in self.get_current_decisions():
            if topic and _classify_topic(val) != topic and _classify_topic(did) != topic:
                continue
            lines.append(f"  - {did}: {val[:100]}")
        if self._reversal_log:
            lines.append("Reversals (superseded):")
            for did, from_c, new_val, to_c in self._reversal_log[-5:]:
                if topic and did != topic and _classify_topic(new_val) != topic:
                    continue
                lines.append(f"  - {did} reversed in chunk {to_c} to: {new_val[:60]}")
        if self.get_conflicts():
            lines.append("Unresolved conflicts:")
            for c in self.get_conflicts():
                vals = " vs ".join(v[0] for v in c.values_mentioned)
                lines.append(f"  - {c.name}: {vals}")
        return "\n".join(lines) if lines else ""

    def generate_final_report(self) -> FinalReport:
        """Call only after receiving END OF MEETING — GENERATE OUTPUT."""
        executive_summary = self._build_executive_summary()
        # Canonical final decisions from stateful layer (current value only)
        final_decisions = [
            state.current_value
            for state in self._decision_states.values()
        ]
        if not final_decisions:
            final_decisions = [d.decision for d in self._decisions if d.reversed_by_chunk_id is None]
        action_items_table = [
            (ai.owner, ai.task, ai.deadline, ai.origin_ts) for ai in self._action_items
        ]
        contradictions = []
        for did, from_chunk, new_val, to_chunk in self._reversal_log:
            contradictions.append(
                f"Decision '{did}' was reversed in chunk {to_chunk} to: \"{new_val[:80]}\""
            )
        for d in self._decisions:
            if d.reversed_by_chunk_id is not None:
                contradictions.append(
                    f"Decision at chunk {d.chunk_id} was reversed in chunk {d.reversed_by_chunk_id}: \"{d.decision[:80]}\""
                )
        for c in self._conflicts:
            if len(c.values_mentioned) >= 2:
                vals = " vs ".join(v[0] for v in c.values_mentioned)
                contradictions.append(f"{c.name}: {vals} ({c.status})")
        # Dedupe by approximate same meaning
        seen_short: set[str] = set()
        unique_contradictions: list[str] = []
        for c in contradictions:
            key = c[:60]
            if key not in seen_short:
                seen_short.add(key)
                unique_contradictions.append(c)
        contradictions = unique_contradictions
        risks = [r[0] for r in self._risks]
        unresolved = [
            i.display_summary or i.topic_key.replace("_", " ")
            for i in self._issues
            if i.status == ISSUE_STATUS_OPEN
        ]
        speaker_matrix: list[tuple[str, str, str]] = []
        for spk, typ, pos, ts, cid in self._speaker_positions:
            speaker_matrix.append((spk, typ, f"[Chunk {cid}] {pos[:80]}"))
        # Timeline: current decision points + one entry per reversal (so QA sees reversal in timeline)
        timeline = [(state.origin_ts, f"{state.id}: {state.current_value}") for state in self._decision_states.values()]
        for did, from_chunk, new_val, to_chunk in self._reversal_log:
            ts = self._chunk_timestamps[to_chunk - 1] if 0 < to_chunk <= len(self._chunk_timestamps) else 0.0
            timeline.append((ts, f"[Reversal] {did} -> {new_val[:60]}"))
        if not timeline:
            timeline = [(d.origin_ts, d.decision) for d in self._decisions if d.reversed_by_chunk_id is None]
        timeline.sort(key=lambda x: x[0])
        logic_inconsistencies = contradictions.copy()
        score, reasoning = self._confidence_score()
        return FinalReport(
            executive_summary=executive_summary,
            final_decisions=final_decisions,
            action_items_table=action_items_table,
            contradictions_identified=contradictions,
            risks_identified=risks,
            unresolved_questions=unresolved,
            speaker_alignment_matrix=speaker_matrix,
            timeline_decision_points=timeline,
            logic_inconsistencies=logic_inconsistencies,
            confidence_score=score,
            confidence_reasoning=reasoning,
        )

    def _build_executive_summary(self) -> str:
        max_words = 300
        parts: list[str] = []
        if self._decisions:
            active = [d for d in self._decisions if d.reversed_by_chunk_id is None]
            if active:
                parts.append("Decisions: " + "; ".join(d.decision for d in active[:5]))
        if self._action_items:
            parts.append(
                "Action items: "
                + "; ".join(f"{ai.owner} - {ai.task[:60]}" for ai in self._action_items[:5])
            )
        if self._risks:
            parts.append("Risks: " + "; ".join(r[0][:80] for r in self._risks[:3]))
        open_only = [i.display_summary or i.topic_key for i in self._issues if i.status == ISSUE_STATUS_OPEN]
        if open_only:
            parts.append("Open: " + "; ".join(open_only[:5]))
        postponed = [i.display_summary or i.topic_key for i in self._issues if i.status == ISSUE_STATUS_POSTPONED]
        if postponed:
            parts.append("Postponed: " + "; ".join(postponed[:3]))
        raw = " ".join(parts)
        words = raw.split()
        if len(words) > max_words:
            raw = " ".join(words[:max_words])
        return raw or "No structured summary could be generated from the transcript."

    def _confidence_score(self) -> tuple[int, str]:
        reasons: list[str] = []
        score = 75
        if not self._raw_chunks:
            return 0, "No transcript chunks processed."
        if any(d.reversed_by_chunk_id is not None for d in self._decisions):
            score -= 5
            reasons.append("Decision reversals detected (explicitly tracked).")
        unresolved_count = sum(1 for i in self._issues if i.status == ISSUE_STATUS_OPEN)
        if unresolved_count > 8:
            score -= 5
            reasons.append("Many open questions.")
        if not self._decisions and not self._action_items:
            score -= 25
            reasons.append("No explicit decisions or action items extracted.")
        high_conf_ai = sum(1 for ai in self._action_items if ai.confidence == "high")
        if self._action_items and high_conf_ai >= len(self._action_items) / 2:
            score += 10
            reasons.append("Most action items have clear owners and/or deadlines.")
        if self._decisions:
            score += 5
            reasons.append("Explicit decisions extracted.")
        low_conf_facts = sum(1 for f in self._facts if f.confidence == "low")
        if low_conf_facts > len(self._facts) * 0.8:
            score -= 5
            reasons.append("Majority of facts are LOW CONFIDENCE.")
        if self._action_items:
            low_ai = sum(1 for ai in self._action_items if ai.confidence == "low")
            if low_ai > len(self._action_items) / 2:
                score -= 5
                reasons.append("Many action items have low-confidence owners.")
        score = max(0, min(100, score))
        if not reasons:
            reasons.append("Structured extraction with no major ambiguities.")
        return score, " ".join(reasons)


def format_memory_update(mu: MemoryUpdate) -> str:
    lines = [
        "[MEMORY UPDATE]",
        "- New facts:",
    ]
    for f in mu.new_facts:
        lines.append(f"  - {f}")
    lines.append("- Updated facts:")
    for f in mu.updated_facts:
        lines.append(f"  - {f}")
    lines.append("- Conflicts detected:")
    for c in mu.conflicts_detected:
        lines.append(f"  - {c}")
    lines.append("- Action items added:")
    for a in mu.action_items_added:
        lines.append(f"  - {a}")
    lines.append("- Decisions updated:")
    for d in mu.decisions_updated:
        lines.append(f"  - {d}")
    lines.append("- Open loops remaining:")
    for o in mu.open_loops_remaining:
        lines.append(f"  - {o}")
    return "\n".join(lines)


def format_final_report(r: FinalReport) -> str:
    sections = [
        "1. Executive Summary (max 300 words)",
        r.executive_summary,
        "",
        "2. Final Decisions",
        *[f"  - {d}" for d in r.final_decisions],
        "",
        "3. Action Items Table (Owner | Task | Deadline | Origin Timestamp)",
    ]
    for owner, task, deadline, ts in r.action_items_table:
        sections.append(f"  - {owner} | {task} | {deadline or 'N/A'} | {ts}")
    sections.extend([
        "",
        "4. Contradictions Identified",
        *[f"  - {c}" for c in r.contradictions_identified],
        "",
        "5. Risks Identified",
        *[f"  - {x}" for x in r.risks_identified],
        "",
        "6. Unresolved Questions",
        *[f"  - {q}" for q in r.unresolved_questions],
        "",
        "7. Speaker Alignment Matrix",
        *[f"  - {spk} | {typ} | {pos}" for spk, typ, pos in r.speaker_alignment_matrix],
        "",
        "8. Timeline of Key Decision Points",
        *[f"  - {ts} | {d}" for ts, d in r.timeline_decision_points],
        "",
        "9. Logic Inconsistencies",
        *[f"  - {x}" for x in r.logic_inconsistencies],
        "",
        f"10. Confidence score: {r.confidence_score}%",
        f"    Reasoning: {r.confidence_reasoning}",
    ])
    return "\n".join(sections)
