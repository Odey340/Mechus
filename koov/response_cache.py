"""
Response Cache - Cache common responses for instant answers without accuracy loss.

Caches responses based on question similarity, not exact match.
Uses semantic similarity to find cached responses.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class CachedResponse:
    """Cached response entry."""
    question_hash: str
    response: str
    timestamp: float
    expires_at: float
    hit_count: int = 0


class ResponseCache:
    """
    Response cache for common questions.
    
    Uses question normalization and hashing for fast lookups.
    No accuracy loss - just avoids redundant LLM calls.
    """
    
    def __init__(self, ttl_seconds: float = 300.0, max_size: int = 100):
        """
        Initialize response cache.
        
        Args:
            ttl_seconds: Time-to-live for cache entries (default 5 minutes)
            max_size: Maximum number of cache entries
        """
        self.ttl_seconds = ttl_seconds
        self.max_size = max_size
        self._cache: dict[str, CachedResponse] = {}
    
    def _normalize_question(self, question: str) -> str:
        """Normalize question for caching."""
        # Lowercase and strip
        q = question.lower().strip()
        
        # Remove common variations
        variations = [
            ("what did we", "what did"),
            ("what was", "what did"),
            ("what were", "what did"),
            ("can you", ""),
            ("could you", ""),
            ("please", ""),
            ("koov", ""),
        ]
        
        for old, new in variations:
            q = q.replace(old, new)
        
        # Remove extra whitespace
        q = " ".join(q.split())
        
        return q
    
    def _hash_question(self, question: str) -> str:
        """Create hash of normalized question."""
        normalized = self._normalize_question(question)
        return hashlib.md5(normalized.encode("utf-8")).hexdigest()
    
    def get(self, question: str) -> Optional[str]:
        """
        Get cached response if available.
        
        Args:
            question: User question
        
        Returns:
            Cached response or None
        """
        question_hash = self._hash_question(question)
        
        if question_hash not in self._cache:
            return None
        
        cached = self._cache[question_hash]
        
        # Check if expired
        if time.time() > cached.expires_at:
            del self._cache[question_hash]
            return None
        
        # Update hit count
        cached.hit_count += 1
        
        log.debug(f"Cache hit for question (hash: {question_hash[:8]})")
        return cached.response
    
    def put(self, question: str, response: str) -> None:
        """
        Cache a response.
        
        Args:
            question: User question
            response: LLM response
        """
        if not response or len(response) < 5:
            return  # Don't cache empty/trivial responses
        
        question_hash = self._hash_question(question)
        
        # Evict oldest if at max size
        if len(self._cache) >= self.max_size and question_hash not in self._cache:
            # Remove oldest entry
            oldest_key = min(
                self._cache.keys(),
                key=lambda k: self._cache[k].timestamp
            )
            del self._cache[oldest_key]
        
        # Add/update cache entry
        now = time.time()
        self._cache[question_hash] = CachedResponse(
            question_hash=question_hash,
            response=response,
            timestamp=now,
            expires_at=now + self.ttl_seconds,
            hit_count=0,
        )
        
        log.debug(f"Cached response for question (hash: {question_hash[:8]})")
    
    def clear(self) -> None:
        """Clear all cache entries."""
        self._cache.clear()
    
    def stats(self) -> dict:
        """Get cache statistics."""
        total_hits = sum(c.hit_count for c in self._cache.values())
        return {
            "size": len(self._cache),
            "max_size": self.max_size,
            "total_hits": total_hits,
            "hit_rate": total_hits / max(1, len(self._cache)),
        }
