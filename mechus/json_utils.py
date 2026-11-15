"""
Safe JSON parsing and structured result for production hardening.

- safe_parse_json: never crash; fallback to regex extraction and structured failure.
- extract_structured_event(text, meta): extraction from text only; event_ids from meta (chunk_id).
- get_structured_qa_state(engine): retrieval from engine; event_ids from engine.get_qa_event_ids().
  Extraction = text in -> structured out. Retrieval = engine state + event_ids. Do not conflate.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mechus.context_integrity_engine import ContextIntegrityEngine

log = logging.getLogger(__name__)

# Key for structured failure in returned dict
PARSE_FAILED_KEY = "_parse_failed"
PARSE_ERROR_KEY = "_parse_error"


def safe_parse_json(text: str) -> dict:
    """
    Parse JSON with fallback repair. Never crash the engine.
    - Try json.loads(text).
    - If fails, extract first JSON object/array with regex.
    - If still fails, return structured failure object {_parse_failed: True, _parse_error: str}.
    """
    if not text or not isinstance(text, str):
        log.warning("safe_parse_json: empty or non-string input")
        return {PARSE_FAILED_KEY: True, PARSE_ERROR_KEY: "empty_or_non_string"}
    text = text.strip()
    # 1) Direct parse
    try:
        out = json.loads(text)
        if isinstance(out, dict):
            return out
        if isinstance(out, list):
            return {"_items": out}
        return {PARSE_FAILED_KEY: True, PARSE_ERROR_KEY: "root_not_object_or_array"}
    except json.JSONDecodeError as e:
        log.debug("safe_parse_json: json.loads failed: %s", e)
    except Exception as e:
        log.warning("safe_parse_json: unexpected error: %s", e, exc_info=True)

    # 2) Extract first {...} or [...]
    try:
        obj_match = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text)
        if obj_match:
            return json.loads(obj_match.group(0))
        arr_match = re.search(r"\[[^\[\]]*(?:\[[^\[\]]*\][^\[\]]*)*\]", text)
        if arr_match:
            return {"_items": json.loads(arr_match.group(0))}
    except json.JSONDecodeError as e:
        log.debug("safe_parse_json: regex-extracted block parse failed: %s", e)
    except Exception as e:
        log.warning("safe_parse_json: repair parse failed: %s", e)

    return {PARSE_FAILED_KEY: True, PARSE_ERROR_KEY: "repair_failed"}


@dataclass
class StructuredResult:
    """Unified result from structured memory for the tiered responder."""
    action_items: list[tuple[str, str, str | None]] = field(default_factory=list)  # (owner, task, deadline)
    decisions: list[tuple[str, str]] = field(default_factory=list)  # (id, value)
    open_questions: list[str] = field(default_factory=list)  # status==open only; display summary
    postponed_questions: list[str] = field(default_factory=list)  # status==postponed; display summary
    event_ids: list[int] = field(default_factory=list)  # chunk ids for grounding
    error: str | None = None  # if set, extraction failed (log and do not use for answer)


def extract_structured_event(text: str, meta: dict | None = None) -> StructuredResult:
    """
    Extract structured data from a single chunk of text. No engine.
    event_ids come from meta (e.g. meta["chunk_id"]); retrieval layer supplies them.
    """
    meta = meta or {}
    chunk_id = meta.get("chunk_id", 0)
    speaker_id = meta.get("speaker_id", "unknown")
    participants = meta.get("participants")
    try:
        from mechus.context_integrity_engine import extract_from_chunk
        action_items, decisions, open_questions = extract_from_chunk(
            text, speaker_id, chunk_id, participants
        )
        event_ids = [chunk_id] if chunk_id else []
        return StructuredResult(
            action_items=action_items,
            decisions=decisions,
            open_questions=open_questions,
            event_ids=event_ids,
        )
    except Exception as e:
        log.exception("extract_structured_event(text) failed: %s", e)
        return StructuredResult(error=str(e))


def get_structured_qa_state(engine: "ContextIntegrityEngine | None") -> StructuredResult:
    """
    Retrieval: get current structured state and event_ids from the engine.
    event_ids come from the retrieval layer (engine.get_qa_event_ids()), not from extraction.
    """
    from mechus.context_integrity_engine import ContextIntegrityEngine  # avoid circular import
    if engine is None:
        return StructuredResult(error="no_engine")
    try:
        items = engine.get_action_items_structured()
        decisions = engine.get_current_decisions()
        open_q = engine.get_open_questions_list()
        postponed_q = engine.get_postponed_issues_list()
        event_ids = engine.get_qa_event_ids()
        return StructuredResult(
            action_items=items,
            decisions=decisions,
            open_questions=open_q,
            postponed_questions=postponed_q,
            event_ids=event_ids,
        )
    except Exception as e:
        log.exception("get_structured_qa_state failed: %s", e)
        return StructuredResult(error=str(e))
