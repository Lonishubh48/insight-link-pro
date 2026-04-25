"""
Context Manager for Insight-Link Pro.

Provides:
- A lightweight TTL-based in-memory cache
- Token / character budget enforcement
- Shared async httpx client lifecycle
"""

import asyncio
import logging
import time
from typing import Any, Optional

import httpx

from .config import config

logger = logging.getLogger(__name__)


class TTLCache:
    """Thread-safe, TTL-based in-memory cache."""

    def __init__(self, ttl: int = 300) -> None:
        self._store: dict[str, tuple[Any, float]] = {}
        self._ttl = ttl
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[Any]:
        """Return cached value if not expired, else None."""
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, ts = entry
            if time.monotonic() - ts > self._ttl:
                del self._store[key]
                return None
            return value

    async def set(self, key: str, value: Any) -> None:
        """Store a value with the current timestamp."""
        async with self._lock:
            self._store[key] = (value, time.monotonic())

    async def invalidate(self, key: str) -> None:
        """Remove a specific cache entry."""
        async with self._lock:
            self._store.pop(key, None)

    async def clear(self) -> None:
        """Flush all cache entries."""
        async with self._lock:
            self._store.clear()

    @property
    def size(self) -> int:
        return len(self._store)


class ContextManager:
    """
    Central resource manager.

    Usage (async context manager):
        async with ContextManager() as ctx:
            client = ctx.http_client
            data = await ctx.cache.get("key")
    """

    def __init__(self) -> None:
        self.cache = TTLCache(ttl=config.cache_ttl_seconds)
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "ContextManager":
        self._client = httpx.AsyncClient(
            timeout=config.request_timeout,
            headers={"User-Agent": f"{config.server_name}/1.0"},
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("ContextManager not entered — use 'async with ContextManager()'.")
        return self._client

    # ------------------------------------------------------------------ #
    # Budget helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def truncate(text: str, label: str = "response") -> str:
        """
        Hard-truncate text to MAX_RESPONSE_CHARS to keep context windows sane.
        Appends a notice so the caller knows truncation happened.
        """
        limit = config.max_response_chars
        if len(text) <= limit:
            return text
        notice = f"\n\n[⚠️  {label} truncated to {limit} chars. Set MAX_RESPONSE_CHARS in .env to increase.]"
        return text[:limit] + notice

    @staticmethod
    def cap_lines(lines: list[str], start: int, end: int) -> list[str]:
        """Return a slice of lines, capped by MAX_FILE_LINES."""
        window = min(end - start, config.max_file_lines)
        return lines[start : start + window]


# Module-level singleton — tools import this directly
context_manager = ContextManager()
cache = context_manager.cache