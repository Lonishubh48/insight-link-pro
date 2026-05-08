"""
Utility helpers for Insight-Link Pro.

Includes:
- Logging setup
- Rate-limit aware retry decorator
- Common text/data formatters
"""

import asyncio
import functools
import logging
import sys
import time
from collections import deque
from typing import Any, Callable, TypeVar

from core.config import config

F = TypeVar("F", bound=Callable[..., Any])

# ------------------------------------------------------------------ #
# Logging
# ------------------------------------------------------------------ #

def setup_logging() -> None:
    """Configure root logger with a clean, structured format."""
    level = getattr(logging, config.log_level.upper(), logging.INFO)
    fmt = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
    datefmt = "%Y-%m-%dT%H:%M:%S"
    # logging.basicConfig(stream=sys.stdout, level=level, format=fmt, datefmt=datefmt)
    logging.basicConfig(stream=sys.stderr, level=level, format=fmt, datefmt=datefmt)
    # Quiet noisy third-party loggers
    for noisy in ("httpx", "httpcore", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ------------------------------------------------------------------ #
# Rate-limit token-bucket
# ------------------------------------------------------------------ #

class RateLimiter:
    """
    Simple sliding-window rate limiter.

    Example:
        limiter = RateLimiter(calls=5, period=1.0)  # 5 calls per second
        await limiter.acquire()
    """

    def __init__(self, calls: int, period: float) -> None:
        self._calls = calls
        self._period = period
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            # Purge timestamps outside the window
            while self._timestamps and now - self._timestamps[0] > self._period:
                self._timestamps.popleft()
            if len(self._timestamps) >= self._calls:
                sleep_for = self._period - (now - self._timestamps[0])
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
            self._timestamps.append(time.monotonic())


# Shared limiters (tune per API)
github_limiter = RateLimiter(calls=10, period=1.0)
jina_limiter = RateLimiter(calls=5, period=1.0)
se_limiter = RateLimiter(calls=3, period=1.0)


# ------------------------------------------------------------------ #
# Retry decorator
# ------------------------------------------------------------------ #

def async_retry(
    max_attempts: int = 3,
    backoff: float = 1.5,
    exceptions: tuple[type[Exception], ...] = (Exception,),
) -> Callable[[F], F]:
    """
    Async retry decorator with exponential back-off.

    Args:
        max_attempts: Maximum number of tries.
        backoff: Multiplier applied to wait between retries.
        exceptions: Exception types that trigger a retry.
    """
    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            logger = logging.getLogger(func.__module__)
            wait = 1.0
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as exc:
                    if attempt == max_attempts:
                        logger.error(
                            "Function %s failed after %d attempts: %s",
                            func.__name__, max_attempts, exc,
                        )
                        raise
                    logger.warning(
                        "Function %s attempt %d/%d failed (%s). Retrying in %.1fs…",
                        func.__name__, attempt, max_attempts, exc, wait,
                    )
                    await asyncio.sleep(wait)
                    wait *= backoff
        return wrapper  # type: ignore[return-value]
    return decorator


# ------------------------------------------------------------------ #
# Text formatters
# ------------------------------------------------------------------ #

def format_file_tree(tree: dict[str, Any], indent: int = 0) -> str:
    """Recursively render a nested dict as an ASCII file tree."""
    lines: list[str] = []
    prefix = "  " * indent
    for name, subtree in sorted(tree.items()):
        if isinstance(subtree, dict):
            lines.append(f"{prefix}📁 {name}/")
            lines.append(format_file_tree(subtree, indent + 1))
        else:
            lines.append(f"{prefix}📄 {name}")
    return "\n".join(filter(None, lines))


def build_error_response(tool: str, error: Exception) -> str:
    """Standardised error message for MCP tools."""
    return (
        f"❌ **{tool}** encountered an error.\n\n"
        f"**Type:** `{type(error).__name__}`\n"
        f"**Detail:** {error}\n\n"
        "_Check your credentials or the target path/URL and try again._"
    )
