"""
services/dedup_cache.py — In-memory TTL-based deduplication cache.

Used by the stream processor to suppress duplicate flow events
that arrive within the same TTL window (e.g. duplicate Tradier ticks).
"""
from __future__ import annotations

import time
from typing import Dict


class DedupCache:
    """Thread-safe*, TTL-based deduplication cache.

    Keys are stored with their insertion timestamp. A key is considered
    a duplicate if it was marked_seen within the last ``ttl_seconds``.

    *Single-threaded asyncio usage only; no Lock needed in that context.

    Args:
        ttl_seconds: How long (in seconds) a key is considered seen.
                     Defaults to 300 (5 minutes).
    """

    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self._ttl = ttl_seconds
        self._store: Dict[str, float] = {}  # key -> insertion epoch

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_duplicate(self, key: str) -> bool:
        """Return True if *key* was marked_seen within the TTL window.

        Does NOT mutate the cache.
        """
        ts = self._store.get(key)
        if ts is None:
            return False
        return (time.monotonic() - ts) < self._ttl

    def mark_seen(self, key: str) -> None:
        """Record *key* as seen at the current time."""
        self._store[key] = time.monotonic()

    def size(self) -> int:
        """Return the number of entries currently in the cache (including expired)."""
        return len(self._store)

    def clear(self) -> None:
        """Remove all entries from the cache."""
        self._store.clear()

    def evict_expired(self) -> int:
        """Remove expired entries and return the count removed."""
        now = time.monotonic()
        expired = [k for k, ts in self._store.items() if (now - ts) >= self._ttl]
        for k in expired:
            del self._store[k]
        return len(expired)
