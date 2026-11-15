"""
Optional web search for Koov Q&A when meeting context is insufficient.
Uses DuckDuckGo Instant Answer API (no API key). Fallback to empty string on failure.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

_DDG_URL = "https://api.duckduckgo.com/"
_TIMEOUT = 5


def web_search_snippet(query: str, max_chars: int = 600) -> str:
    """
    Fetch a short text snippet for the query from DuckDuckGo Instant Answer API.
    Returns empty string on failure or if no abstract/results.
    """
    if not (query or "").strip():
        return ""
    q = query.strip()[:200]
    try:
        url = _DDG_URL + "?" + urllib.parse.urlencode({"q": q, "format": "json", "no_redirect": "1"})
        req = urllib.request.Request(url, headers={"User-Agent": "KoovMeetingBot/1.0"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log.debug("Web search failed: %s", exc)
        return ""

    parts: list[str] = []
    abstract = data.get("AbstractText") or data.get("Abstract") or ""
    if abstract:
        parts.append(abstract.strip())
    source = data.get("AbstractSource")
    if source:
        parts.append(f"(Source: {source})")

    for item in data.get("RelatedTopics", [])[:2]:
        if isinstance(item, dict) and item.get("Text"):
            parts.append(item["Text"].strip())
        elif isinstance(item, str):
            parts.append(item.strip())
        if sum(len(p) for p in parts) >= max_chars:
            break

    result = " ".join(parts).strip()
    if len(result) > max_chars:
        result = result[: max_chars - 3] + "..."
    return result


def question_needs_web(request_lower: str, has_meeting_context: bool) -> bool:
    """Return True if the question likely needs external/web info."""
    if has_meeting_context:
        need_web_phrases = [
            "look up", "search for", "find out about", "from the internet",
            "what is ", "who is ", "define ", "meaning of ", "explain what is ",
        ]
        return any(p in request_lower for p in need_web_phrases)
    return True
