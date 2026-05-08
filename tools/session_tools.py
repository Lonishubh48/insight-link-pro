"""
Session Memory Tools
=====================
MCP tools that expose session memory to Claude during a conversation.

Tools:
    - get_session_context : Retrieve a formatted summary of the current session.
    - clear_session       : Reset the current session back to empty state.

HOW THIS FITS IN THE PROJECT:
    core/session_store.py  -> holds the data and update methods
    tools/session_tools.py -> exposes that data as MCP tools (YOU ARE HERE)
    tools/repo_tools.py    -> writes to session after map_repository runs
    tools/doc_tools.py     -> writes to session after web fetches run
    main.py                -> calls register_session_tools(mcp) at startup

WHEN CLAUDE USES THESE TOOLS:
    User: "Which files did you already read?"
    Claude calls: get_session_context()
    Claude sees: full list of inspected files, scanned repos, searches done
    Claude answers: without re-running any expensive operations

    User: "Start fresh, forget everything"
    Claude calls: clear_session()
    Claude sees: confirmation that memory was wiped
"""

import logging

from fastmcp import FastMCP

from core.session_store import session_store
from utils.helpers import build_error_response

logger = logging.getLogger(__name__)


def register_session_tools(mcp: FastMCP) -> None:
    """Register all session memory tools on the given FastMCP instance."""

    @mcp.tool()
    async def get_session_context(session_id: str = "default") -> str:
        """
        Retrieve a formatted summary of everything done in the current session.

        Use this tool at the start of a follow-up question to recall:
            - Which repository was last scanned
            - Which files were already read and at which line ranges
            - Which URLs were already fetched
            - Which Stack Overflow searches were already performed
            - A chronological timeline of all actions taken

        This avoids redundant re-scanning and re-fetching, making
        follow-up answers faster and more context-aware.

        Args:
            session_id: The session identifier to retrieve.
                        Use "default" for standard single-user Claude Desktop usage.
                        Pass a custom ID if running multiple parallel sessions.

        Returns:
            Formatted Markdown summary of the session context,
            or an error message if retrieval fails.
        """
        try:
            summary = await session_store.get_summary(session_id)
            logger.debug(
                "get_session_context called for session '%s'. "
                "Active sessions: %d",
                session_id,
                session_store.active_session_count,
            )
            return summary

        except Exception as exc:
            logger.exception("get_session_context failed for session '%s'", session_id)
            return build_error_response("get_session_context", exc)

    @mcp.tool()
    async def clear_session(session_id: str = "default") -> str:
        """
        Reset the current session memory back to an empty state.

        Use this when:
            - Starting work on a completely different repository
            - The session context has become stale or irrelevant
            - The user explicitly asks to "start fresh" or "forget everything"

        This does NOT restart the MCP server. It only clears the
        in-memory record of what was done in this session.
        The server continues running normally after clearing.

        Args:
            session_id: The session identifier to clear.
                        Defaults to "default".

        Returns:
            Confirmation message indicating the session was cleared,
            or a notice if the session was already empty.
        """
        try:
            result = await session_store.clear_session(session_id)
            logger.info(
                "Session '%s' cleared. Active sessions remaining: %d",
                session_id,
                session_store.active_session_count,
            )
            return result

        except Exception as exc:
            logger.exception("clear_session failed for session '%s'", session_id)
            return build_error_response("clear_session", exc)