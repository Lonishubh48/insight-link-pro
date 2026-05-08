"""
Session Store — Insight-Link Pro
==================================
Provides persistent-within-process session memory for the MCP server.

WHY THIS EXISTS:
    MCP servers are stateless by default — every tool call is independent.
    This module gives Claude a "working memory" within a session so it can:
        - Remember which repo was last scanned (avoid re-scanning)
        - Track which files were already inspected
        - Recall previous search queries and their results
        - Build a timeline of actions for context-aware follow-up answers

DESIGN DECISIONS:
    - Sessions are identified by a session_id string (default: "default")
    - Each session is a SessionContext dataclass (typed, not a raw dict)
    - SessionStore wraps a dict of session_id → SessionContext
    - TTL: sessions expire after SESSION_TTL_SECONDS (default 30 min)
    - Thread-safe: uses asyncio.Lock for concurrent access
    - No external dependencies: pure Python, no Redis/DB needed

HOW IT FITS IN THE PROJECT:
    core/session_store.py    ← YOU ARE HERE
    tools/repo_tools.py      → calls session_store.update_repo_scan()
    tools/doc_tools.py       → calls session_store.update_web_fetch()
    tools/session_tools.py   → exposes get_session_context() as MCP tool
    main.py                  → imports session_store singleton
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# How long a session lives without any activity (seconds)
# Default: 30 minutes — long enough for a deep coding session
SESSION_TTL_SECONDS = 1800


# ------------------------------------------------------------------ #
# DATA STRUCTURES
# ------------------------------------------------------------------ #

@dataclass
class InspectedFile:
    """
    Represents a single file that was read via inspect_code.

    Stored in SessionContext.inspected_files so Claude can recall
    "I already read lines 1-50 of main.py" without re-fetching.
    """
    file_path: str          # Absolute path to the file
    start_line: int         # First line that was read
    end_line: int           # Last line that was read
    timestamp: float        # When it was read (Unix time)
    line_count: int         # Total lines in the file


@dataclass
class WebFetch:
    """
    Represents a URL that was fetched via web_to_markdown.

    Stored so Claude knows "I already fetched the FastAPI docs"
    and can reference them without re-fetching.
    """
    url: str                # The URL that was fetched
    timestamp: float        # When it was fetched
    char_count: int         # How much content was returned


@dataclass
class SOSearch:
    """
    Represents a Stack Overflow search that was performed.

    Stored so Claude knows "I already searched for this error"
    and can reference previous results in follow-up answers.
    """
    query: str              # The search query
    timestamp: float        # When the search was run
    result_count: int       # How many results were returned


@dataclass
class SessionContext:
    """
    The complete memory of a single user session.

    Think of this as Claude's "notepad" for an ongoing conversation.
    Every tool call updates this notepad so follow-up questions
    can be answered without repeating expensive operations.

    Fields:
        session_id:         Unique identifier for this session
        created_at:         When the session started
        last_active:        Last time any tool was called (used for TTL)
        active_repo:        The repo most recently scanned with map_repository
        repo_tree_summary:  The file tree returned by map_repository
        inspected_files:    List of files read with inspect_code
        web_fetches:        List of URLs fetched with web_to_markdown
        so_searches:        List of Stack Overflow searches performed
        action_timeline:    Chronological log of all tool calls this session
    """
    session_id: str
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)

    # Repository context
    active_repo: str = ""               # e.g. "F:/myproject"
    repo_tree_summary: str = ""         # The ASCII tree from map_repository

    # Ingestion history
    inspected_files: list[InspectedFile] = field(default_factory=list)
    web_fetches: list[WebFetch] = field(default_factory=list)
    so_searches: list[SOSearch] = field(default_factory=list)

    # Human-readable timeline of actions
    # e.g. ["10:32 - Scanned repo: myproject", "10:33 - Read main.py lines 1-50"]
    action_timeline: list[str] = field(default_factory=list)

    def touch(self) -> None:
        """Update last_active to now — called on every tool use."""
        self.last_active = time.time()

    def is_expired(self) -> bool:
        """Return True if the session has been idle longer than SESSION_TTL_SECONDS."""
        return (time.time() - self.last_active) > SESSION_TTL_SECONDS

    def add_action(self, description: str) -> None:
        """
        Append a human-readable action to the timeline.

        Args:
            description: Short description e.g. "Scanned repo: myproject"
        """
        # Format: "HH:MM - description"
        timestamp_str = time.strftime("%H:%M", time.localtime())
        entry = f"{timestamp_str} - {description}"
        self.action_timeline.append(entry)

        # Keep timeline from growing unbounded — last 50 actions is plenty
        if len(self.action_timeline) > 50:
            self.action_timeline = self.action_timeline[-50:]

    def format_summary(self) -> str:
        """
        Render the session context as a clean Markdown summary.

        This is what Claude sees when it calls get_session_context().
        The output is designed to give Claude maximum useful context
        in minimum tokens.

        Returns:
            Formatted Markdown string summarising the session.
        """
        lines: list[str] = [
            "# Session Memory\n",
            f"**Session ID:** `{self.session_id}`",
            f"**Started:** {time.strftime('%H:%M:%S', time.localtime(self.created_at))}",
            f"**Last Active:** {time.strftime('%H:%M:%S', time.localtime(self.last_active))}",
        ]

        # --- Active Repository ---
        if self.active_repo:
            lines.append(f"\n## Active Repository\n`{self.active_repo}`")
            if self.repo_tree_summary:
                # Show just the first 20 lines of the tree to save tokens
                tree_preview = "\n".join(
                    self.repo_tree_summary.splitlines()[:20]
                )
                lines.append(f"\n**Tree Preview:**\n```\n{tree_preview}\n```")
        else:
            lines.append("\n## Active Repository\n_No repository scanned yet._")

        # --- Inspected Files ---
        if self.inspected_files:
            lines.append(f"\n##  Files Already Read ({len(self.inspected_files)})")
            for f in self.inspected_files[-10:]:  # Show last 10 only
                ts = time.strftime("%H:%M", time.localtime(f.timestamp))
                lines.append(
                    f"- `{f.file_path}` "
                    f"lines {f.start_line}–{f.end_line} "
                    f"of {f.line_count} "
                    f"_(at {ts})_"
                )
        else:
            lines.append("\n## Files Already Read\n_No files inspected yet._")

        # --- Web Fetches ---
        if self.web_fetches:
            lines.append(f"\n##  URLs Already Fetched ({len(self.web_fetches)})")
            for w in self.web_fetches[-5:]:  # Show last 5 only
                ts = time.strftime("%H:%M", time.localtime(w.timestamp))
                lines.append(f"- [{w.url}]({w.url}) _(at {ts}, {w.char_count} chars)_")
        else:
            lines.append("\n## URLs Already Fetched\n_No URLs fetched yet._")

        # --- Stack Overflow Searches ---
        if self.so_searches:
            lines.append(f"\n##  Stack Overflow Searches ({len(self.so_searches)})")
            for s in self.so_searches[-5:]:  # Show last 5 only
                ts = time.strftime("%H:%M", time.localtime(s.timestamp))
                lines.append(
                    f"- `{s.query}` "
                    f"→ {s.result_count} results "
                    f"_(at {ts})_"
                )
        else:
            lines.append("\n## Stack Overflow Searches\n_No searches performed yet._")

        # --- Action Timeline ---
        if self.action_timeline:
            lines.append(f"\n## Action Timeline (last {min(10, len(self.action_timeline))} actions)")
            for action in self.action_timeline[-10:]:
                lines.append(f"- {action}")

        return "\n".join(lines)


# ------------------------------------------------------------------ #
# SESSION STORE
# ------------------------------------------------------------------ #

class SessionStore:
    """
    Central registry of all active sessions.

    Maintains a dict of session_id → SessionContext.
    Handles creation, retrieval, updates, expiry, and cleanup.

    Usage:
        # Get or create a session
        ctx = await session_store.get_or_create("default")

        # Update after a repo scan
        await session_store.update_repo_scan(
            session_id="default",
            repo_path="F:/myproject",
            tree_summary=" core/\n   config.py..."
        )

        # Get formatted summary for Claude
        summary = await session_store.get_summary("default")
    """

    def __init__(self) -> None:
        # Dict mapping session_id → SessionContext
        self._sessions: dict[str, SessionContext] = {}
        # asyncio lock prevents race conditions when multiple tools
        # update the same session simultaneously
        self._lock = asyncio.Lock()

    async def get_or_create(self, session_id: str = "default") -> SessionContext:
        """
        Retrieve an existing session or create a new one.

        If the session exists but has expired (idle > SESSION_TTL_SECONDS),
        it is automatically reset to a fresh state.

        Args:
            session_id: Identifier for the session. Defaults to "default"
                        which is fine for single-user Claude Desktop usage.

        Returns:
            The SessionContext for the given session_id.
        """
        async with self._lock:
            existing = self._sessions.get(session_id)

            if existing is not None:
                if existing.is_expired():
                    # Session expired — start fresh, log the reset
                    logger.info(
                        "Session '%s' expired after %.1f min idle. Resetting.",
                        session_id,
                        (time.time() - existing.last_active) / 60,
                    )
                    self._sessions[session_id] = SessionContext(session_id=session_id)
                else:
                    # Valid session — update last_active and return
                    existing.touch()
                return self._sessions[session_id]

            # Brand new session
            logger.info("Creating new session: '%s'", session_id)
            session = SessionContext(session_id=session_id)
            self._sessions[session_id] = session
            return session

    async def update_repo_scan(
        self,
        repo_path: str,
        tree_summary: str,
        session_id: str = "default",
    ) -> None:
        """
        Record that a repository was scanned via map_repository.

        Called by repo_tools.map_repository() after a successful scan.

        Args:
            repo_path:    Absolute path to the repository root.
            tree_summary: The formatted file tree string.
            session_id:   Target session (default: "default").
        """
        ctx = await self.get_or_create(session_id)
        async with self._lock:
            ctx.active_repo = repo_path
            ctx.repo_tree_summary = tree_summary
            ctx.add_action(f"Scanned repository: `{repo_path}`")
            logger.debug("Session '%s': repo scan recorded for %s", session_id, repo_path)

    async def update_file_inspection(
        self,
        file_path: str,
        start_line: int,
        end_line: int,
        line_count: int,
        session_id: str = "default",
    ) -> None:
        """
        Record that a file was read via inspect_code.

        Called by repo_tools.inspect_code() after successfully reading a file.

        Args:
            file_path:  Absolute path to the file that was read.
            start_line: First line that was read.
            end_line:   Last line that was read.
            line_count: Total lines in the file.
            session_id: Target session (default: "default").
        """
        ctx = await self.get_or_create(session_id)
        async with self._lock:
            ctx.inspected_files.append(InspectedFile(
                file_path=file_path,
                start_line=start_line,
                end_line=end_line,
                timestamp=time.time(),
                line_count=line_count,
            ))
            # Keep list bounded — last 30 file reads is plenty
            if len(ctx.inspected_files) > 30:
                ctx.inspected_files = ctx.inspected_files[-30:]
            ctx.add_action(
                f"Read `{file_path}` lines {start_line}–{end_line}"
            )

    async def update_web_fetch(
        self,
        url: str,
        char_count: int,
        session_id: str = "default",
    ) -> None:
        """
        Record that a URL was fetched via web_to_markdown.

        Args:
            url:        The URL that was fetched.
            char_count: Number of characters in the returned content.
            session_id: Target session (default: "default").
        """
        ctx = await self.get_or_create(session_id)
        async with self._lock:
            ctx.web_fetches.append(WebFetch(
                url=url,
                timestamp=time.time(),
                char_count=char_count,
            ))
            if len(ctx.web_fetches) > 20:
                ctx.web_fetches = ctx.web_fetches[-20:]
            ctx.add_action(f"Fetched URL: {url}")

    async def update_so_search(
        self,
        query: str,
        result_count: int,
        session_id: str = "default",
    ) -> None:
        """
        Record that a Stack Overflow search was performed.

        Args:
            query:        The search query string.
            result_count: Number of results returned.
            session_id:   Target session (default: "default").
        """
        ctx = await self.get_or_create(session_id)
        async with self._lock:
            ctx.so_searches.append(SOSearch(
                query=query,
                timestamp=time.time(),
                result_count=result_count,
            ))
            if len(ctx.so_searches) > 20:
                ctx.so_searches = ctx.so_searches[-20:]
            ctx.add_action(f"Searched Stack Overflow: `{query}`")

    async def get_summary(self, session_id: str = "default") -> str:
        """
        Return a formatted Markdown summary of the session.

        This is what the get_session_context MCP tool returns to Claude.

        Args:
            session_id: Target session (default: "default").

        Returns:
            Formatted Markdown string.
        """
        ctx = await self.get_or_create(session_id)
        return ctx.format_summary()

    async def clear_session(self, session_id: str = "default") -> str:
        """
        Reset a session back to empty state.

        Called by the clear_session MCP tool when the user wants
        to start fresh without restarting the server.

        Args:
            session_id: Target session to clear (default: "default").

        Returns:
            Confirmation message.
        """
        async with self._lock:
            if session_id in self._sessions:
                del self._sessions[session_id]
                logger.info("Session '%s' cleared by user.", session_id)
                return f" Session `{session_id}` cleared. Starting fresh."
            return f"ℹ  Session `{session_id}` was already empty."

    async def cleanup_expired(self) -> int:
        """
        Remove all expired sessions from memory.

        Can be called periodically to prevent memory leaks in
        long-running server instances.

        Returns:
            Number of sessions that were removed.
        """
        async with self._lock:
            expired = [
                sid for sid, ctx in self._sessions.items()
                if ctx.is_expired()
            ]
            for sid in expired:
                del self._sessions[sid]
                logger.info("Cleaned up expired session: '%s'", sid)
            return len(expired)

    @property
    def active_session_count(self) -> int:
        """Return the number of currently active (non-expired) sessions."""
        return sum(1 for ctx in self._sessions.values() if not ctx.is_expired())


# ------------------------------------------------------------------ #
# MODULE-LEVEL SINGLETON
#
# All tools import this single instance so they all share
# the same session state. This is safe because:
# - Python modules are singletons (imported once, cached)
# - All mutations go through asyncio.Lock
# ------------------------------------------------------------------ #
session_store = SessionStore()
