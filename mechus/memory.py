from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import time
import re
from typing import Any, Dict, Optional

from mechus.text import normalize_text


@dataclass
class TopicContext:
    """Represents a topic bubble containing all segments about a specific topic."""
    topic_id: str
    keywords: list[str]
    segments: list[tuple[float, str]] = field(default_factory=list)
    last_updated: float = field(default_factory=time.time)
    total_chars: int = 0
    
    def add_segment(self, ts: float, text: str) -> None:
        self.segments.append((ts, text))
        self.total_chars += len(text)
        self.last_updated = max(self.last_updated, ts)
    
    def get_text(self) -> str:
        return " ".join(seg for _, seg in self.segments).strip()

    def get_recent_text(self, max_chars: int = 2000) -> str:
        """Return the most recent segments up to max_chars (for long meetings)."""
        if not self.segments or max_chars <= 0:
            return ""
        out: list[str] = []
        total = 0
        for _, seg in reversed(self.segments):
            if total + len(seg) + 1 > max_chars and out:
                break
            out.append(seg)
            total += len(seg) + 1
        out.reverse()
        return " ".join(out).strip()


@dataclass
class ConversationMemory:
    max_chars: int
    recent_chars: int

    def __post_init__(self) -> None:
        self._segments: list[tuple[float, str]] = []
        self._total_chars = 0
        self._topic_contexts: dict[str, TopicContext] = {}
        self._topic_keywords_cache: dict[str, list[str]] = {}

    def _extract_keywords(self, text: str, max_keywords: int = 5) -> list[str]:
        """Extract meaningful keywords from text for topic detection."""
        text_l = normalize_text(text).lower()
        if not text_l:
            return []
        
        # Cache keywords for segments
        if text in self._topic_keywords_cache:
            return self._topic_keywords_cache[text]
        
        # Remove common stopwords, pronouns, and filler words
        stopwords = {
            "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with",
            "by", "from", "as", "is", "are", "was", "were", "be", "been", "being", "have",
            "has", "had", "do", "does", "did", "will", "would", "should", "could", "may",
            "might", "can", "this", "that", "these", "those", "i", "you", "he", "she", "it",
            "we", "they", "what", "which", "who", "where", "when", "why", "how", "said",
            "say", "says", "mechus", "just", "now", "then", "here", "there", "about", "into",
            "like", "um", "uh", "hmm", "yeah", "sure", "good", "better", "nice", "cool",
        }
        
        keywords = []
        
        # Prioritize capitalized terms (UGC, API, DNS, etc.)
        caps_terms = re.findall(r'\b[A-Z]{2,}\b', text)
        keywords.extend([t.lower() for t in caps_terms])
        
        # Get longer meaningful words (4+ chars)
        words = re.findall(r'\b[a-z]{4,}\b', text_l)
        for word in words:
            if word not in stopwords and word not in keywords:
                keywords.append(word)
        
        # Score and sort by importance (longer words, proper nouns get priority)
        scored = []
        for kw in keywords:
            score = len(kw)  # Longer words score higher
            if any(c.isupper() for c in text if kw in text.lower()):  # Proper noun boost
                score += 5
            scored.append((score, kw))
        
        scored.sort(reverse=True)
        result = [kw for _, kw in scored[:max_keywords]]
        self._topic_keywords_cache[text] = result
        return result
    
    def _find_matching_topic(self, keywords: list[str]) -> Optional[str]:
        """Find an existing topic that matches the keywords."""
        if not keywords:
            return None
        
        best_match = None
        best_score = 0.0
        
        for topic_id, topic_ctx in self._topic_contexts.items():
            # Calculate overlap between new keywords and topic keywords
            overlap = len(set(keywords) & set(topic_ctx.keywords))
            if overlap > 0:
                # Score based on overlap and recency
                age = time.time() - topic_ctx.last_updated
                recency_bonus = 1.0 / (1.0 + age / 600.0)  # 10 min half-life
                score = overlap * (1.0 + recency_bonus)
                if score > best_score:
                    best_score = score
                    best_match = topic_id
        
        # Only match if there's significant overlap (at least 2 keywords or 1 strong match)
        if best_score >= 1.5:
            return best_match
        return None
    
    def _create_topic_id(self, keywords: list[str]) -> str:
        """Create a topic ID from keywords."""
        if not keywords:
            return "general"
        # Use top 2-3 keywords to create ID
        key = "_".join(sorted(keywords[:3]))
        return key
    
    def add_segment(self, text: str) -> None:
        text = normalize_text(text)
        if not text:
            return
        
        # Remove transcript refiner artifacts before storing
        text = re.sub(
            r"(Sure!?\s*)?(Here is|Here's)\s+(the\s+)?(cleaned\s+)?(transcript|version):\s*",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = text.strip('"').strip()
        text = normalize_text(text)
        
        if not text:
            return
        
        ts = time.time()
        
        # Add to flat segments list (for backward compatibility)
        self._segments.append((ts, text))
        self._total_chars += len(text)
        
        # Extract keywords and assign to topic
        keywords = self._extract_keywords(text)
        topic_id = self._find_matching_topic(keywords)
        
        if topic_id is None:
            # Create new topic
            topic_id = self._create_topic_id(keywords)
            if topic_id not in self._topic_contexts:
                self._topic_contexts[topic_id] = TopicContext(
                    topic_id=topic_id,
                    keywords=keywords,
                )
        
        # Add segment to topic context
        self._topic_contexts[topic_id].add_segment(ts, text)
        
        # Trim old topics and segments
        self._trim_to_max()

    def _trim_to_max(self) -> None:
        """Trim both flat segments and topic contexts to stay within limits."""
        # Trim flat segments
        while self._total_chars > self.max_chars and self._segments:
            _, dropped = self._segments.pop(0)
            self._total_chars -= len(dropped)
        
        # Trim old/inactive topics (keep only recent ones)
        now = time.time()
        max_topic_age = 3600  # 1 hour
        topics_to_remove = []
        for topic_id, topic_ctx in self._topic_contexts.items():
            if (now - topic_ctx.last_updated) > max_topic_age and topic_ctx.total_chars < 100:
                topics_to_remove.append(topic_id)
        
        for topic_id in topics_to_remove:
            del self._topic_contexts[topic_id]

    def _joined(self, segments: list[tuple[float, str]]) -> str:
        return " ".join(seg for _, seg in segments if seg).strip()

    def get_recent(self) -> str:
        if not self._segments:
            return ""
        budget = max(1, self.recent_chars)
        picked: list[tuple[float, str]] = []
        total = 0
        for ts, seg in reversed(self._segments):
            if not seg:
                continue
            seg_len = len(seg)
            if picked and (total + seg_len) > budget:
                break
            picked.append((ts, seg))
            total += seg_len + 1
            if total >= budget:
                break
        picked.reverse()
        return self._joined(picked)

    def retrieve(self, request_text: str, now_s: float | None = None) -> str:
        """Return memory using topic-based context bubbles when relevant."""
        if not self._segments:
            return ""
        if now_s is None:
            now_s = time.time()

        request = normalize_text(request_text or "")
        request_l = request.lower()
        
        # Extract keywords from request
        request_keywords = self._extract_keywords(request, max_keywords=10)
        # Boost topic-specific retrieval: add explicit topic terms (including short ones like "api")
        topic_seed = [
            "pricing", "api", "hiring", "timeline", "budget", "launch", "v1", "v2",
            "tier", "tiers", "enterprise", "backfill", "slate", "march", "june", "may",
            "objection", "objections", "competitor", "demo", "contract", "discount",
            "acme", "quarter", "renewal", "onboarding", "implementation", "next steps",
        ]
        for term in topic_seed:
            if term in request_l and term not in request_keywords:
                request_keywords.append(term)
        
        # Determine query type
        recent_focus = any(p in request_l for p in ["just happened", "just now", "recently", "right now", "what just"])
        holistic_focus = any(p in request_l for p in ["summary", "overall", "recap", "so far", "everything", "whole meeting", "entire meeting"])
        topic_specific = any(p in request_l for p in ["about", "regarding", "concerning"]) or request_keywords
        is_decision = any(p in request_l for p in ["decide", "decision", "should we", "what should", "recommend", "suggest", "just decide"])
        is_specific_question = any(t in request_l for t in ["what", "who", "when", "where", "why", "how"]) and not holistic_focus
        is_who_question = "who " in request_l and any(p in request_l for p in ["raised", "said", "mentioned", "brought"])

        # For holistic summaries, return all topics
        if holistic_focus:
            all_topics = []
            for topic_ctx in sorted(self._topic_contexts.values(), key=lambda t: t.last_updated, reverse=True):
                topic_text = topic_ctx.get_text()
                if topic_text:
                    all_topics.append(f"[Topic: {', '.join(topic_ctx.keywords[:3])}] {topic_text}")
            if all_topics:
                return "\n\n".join(all_topics)
            # Fallback to flat segments
            return self._get_flat_retrieval(request_text, now_s, holistic_focus, recent_focus, is_specific_question, is_who_question)
        
        # For topic-specific queries, find matching topic bubbles
        if topic_specific and request_keywords:
            # If the question names a topic (e.g. "about the API"), only return segments that mention that topic
            asked_topic_terms = [t for t in topic_seed if t in request_l]
            matching_topics = []
            for topic_ctx in self._topic_contexts.values():
                topic_text = topic_ctx.get_text()
                topic_l = topic_text.lower()
                if asked_topic_terms and not any(t in topic_l for t in asked_topic_terms):
                    continue
                overlap = len(set(request_keywords) & set(topic_ctx.keywords))
                if overlap > 0:
                    kw_in_text = sum(1 for kw in request_keywords if kw in topic_l)
                    age = now_s - topic_ctx.last_updated
                    recency_score = 1.0 / (1.0 + age / 600.0)
                    relevance = overlap * (1.0 + recency_score) + kw_in_text * 2.0
                    matching_topics.append((relevance, topic_ctx))
            
            if matching_topics:
                matching_topics.sort(reverse=True, key=lambda x: x[0])
                selected_topics = []
                total_chars = 0
                budget = int(self.recent_chars * 2.5) if is_decision else (int(self.recent_chars * 1.4) if is_specific_question else self.recent_chars)
                # Long meeting: cap each topic so we get recent, relevant slice (avoids overflow + stale content)
                long_meeting = len(self._segments) > 35
                per_topic_cap = min(2200, budget // 2) if long_meeting else None
                
                for relevance, topic_ctx in matching_topics:
                    if per_topic_cap:
                        topic_text = topic_ctx.get_recent_text(max_chars=per_topic_cap)
                    else:
                        topic_text = topic_ctx.get_text()
                    if topic_text:
                        topic_len = len(topic_text)
                        if total_chars + topic_len <= budget:
                            selected_topics.append(topic_text)
                            total_chars += topic_len
                        elif is_decision:
                            selected_topics.append(topic_text)
                            break
                
                if selected_topics:
                    return "\n\n".join(selected_topics)
        
        # Fallback to flat segment retrieval
        return self._get_flat_retrieval(request_text, now_s, holistic_focus, recent_focus, is_specific_question, is_who_question)
    
    def _get_flat_retrieval(self, request_text: str, now_s: float, holistic_focus: bool, recent_focus: bool, is_specific_question: bool, is_who_question: bool = False) -> str:
        """Fallback to flat segment-based retrieval."""
        request_l = request_text.lower()
        tokens = []
        caps_terms = re.findall(r'\b[A-Z]{2,}\b', request_text)
        tokens.extend([t.lower() for t in caps_terms])
        words = re.findall(r'\b[a-z]{4,}\b', request_l)
        tokens.extend([w for w in words if w not in {"mechus", "what", "did", "the", "think", "should", "would", "could", "about", "with", "that", "this", "they", "them", "their", "have", "been", "were", "said", "from"}])
        tokens = list(set(tokens))

        keyword_weight = 6.0 if (is_specific_question and is_who_question) else (5.0 if is_specific_question else 2.0)
        is_decision = any(p in request_text.lower() for p in ["decide", "decision", "just decide"])
        budget = self.recent_chars
        if holistic_focus:
            budget = min(self.max_chars, max(self.recent_chars * 2, 2500))
        elif is_decision:
            budget = min(self.max_chars, int(self.recent_chars * 2.2))
        elif is_specific_question:
            budget = min(self.max_chars, int(self.recent_chars * 1.8))

        recency_window_s = 300 if recent_focus else None

        scored: list[tuple[float, float, str]] = []
        for ts, seg in self._segments:
            if not seg:
                continue
            age = max(0.0, now_s - ts)
            recency_score = 1.0 / (1.0 + age / 300.0)
            seg_l = seg.lower()
            keyword_hits = sum(1 for t in tokens if t in seg_l)
            score = recency_score * (1.5 if recent_focus else 1.0) + keyword_hits * keyword_weight
            scored.append((score, ts, seg))

        scored.sort(key=lambda x: x[0], reverse=True)

        chosen: list[tuple[float, str]] = []
        total_chars = 0
        for score, ts, seg in scored:
            if recency_window_s is not None and (now_s - ts) > recency_window_s:
                continue
            seg_len = len(seg)
            if chosen and (total_chars + seg_len) > budget:
                continue
            chosen.append((ts, seg))
            total_chars += seg_len + 1
            if total_chars >= budget:
                break

        if not chosen:
            return self.get_recent()

        chosen.sort(key=lambda x: x[0])
        return self._joined(chosen)


@dataclass
class Note:
    timestamp_s: float
    kind: str
    text: str
    meta: Dict[str, Any]

    def to_json(self) -> str:
        payload = {
            "ts": self.timestamp_s,
            "kind": self.kind,
            "text": self.text,
        }
        if self.meta:
            payload.update(self.meta)
        return json.dumps(payload, ensure_ascii=False)


class NoteStore:
    """
    Session-scoped JSONL log under ``{root_dir}/{session_id}.jsonl`` (e.g. mechus_notes/).

    Used for live dashboard transcript (``kind=transcript`` with rich ``meta``: segments, STT tags,
    ``llm_context``) and wake cycles. This is the path exposed in worker status for ``GET /api/transcript``.

    **Policy:** For a given ``meeting_id``, **MeetingStore** (``MECHUS_MEETING_STORE_DIR`` / ``mechus_meetings``)
    is the canonical durable record for replay, enriched chunks (``chunk_id``, engine ``tags``/``topic``),
    snapshots, and ``final_summary.json``. NoteStore remains the API-facing session log for the same run;
    engine-level tags on disk live in MeetingStore ``chunks.jsonl``.
    """

    def __init__(self, root_dir: str, session_id: str) -> None:
        self._root_dir = root_dir
        self._session_id = session_id
        os.makedirs(self._root_dir, exist_ok=True)
        self._path = os.path.join(self._root_dir, f"{self._session_id}.jsonl")

    @property
    def path(self) -> str:
        return self._path

    def add(self, kind: str, text: str, meta: Optional[Dict[str, Any]] = None) -> None:
        text = normalize_text(text)
        if not text:
            return
        note = Note(timestamp_s=time.time(), kind=kind, text=text, meta=meta or {})
        line = note.to_json()
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(line)
            f.write("\n")
            f.flush()

    def sync(self) -> None:
        """Best-effort fsync so the session JSONL is durable before process exit."""
        if not os.path.isfile(self._path):
            return
        try:
            fd = os.open(self._path, os.O_RDWR)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass

    def read_notes(self, kind: Optional[str] = None) -> list[Dict[str, Any]]:
        """Read all notes from the store. If kind is set, only return notes with that kind."""
        if not os.path.isfile(self._path):
            return []
        out = []
        with open(self._path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if kind is not None and obj.get("kind") != kind:
                        continue
                    out.append(obj)
                except (json.JSONDecodeError, TypeError):
                    continue
        return out
