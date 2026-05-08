"""
Insight-Link Pro — MCP Server
==============================
Entry point. Assembles the FastMCP server, registers all tools,
and runs via stdio (default) or SSE transport.

Usage:
    python main.py                          # stdio (Claude Desktop / MCP clients)
    python main.py --transport sse          # HTTP SSE server on port 8000
    python main.py --transport sse --port 9000
"""

import argparse
import logging
import sys

from fastmcp import FastMCP

from core.config import config
from tools import register_analysis_tools, register_doc_tools, register_repo_tools, register_session_tools
from utils.helpers import setup_logging

# ------------------------------------------------------------------ #
# Bootstrap
# ------------------------------------------------------------------ #

setup_logging()
logger = logging.getLogger(__name__)

# Total tools: map_repository, inspect_code, web_to_markdown,
#              search_stack_overflow, analyze_issues, dependency_checker
#              get_session_context, clear_session
_TOOL_COUNT = 8


def create_server() -> FastMCP:
    """
    Instantiate and fully configure the FastMCP server.

    Returns:
        A ready-to-run FastMCP instance with all tools registered.
    """
    mcp = FastMCP(
        name=config.server_name,
        instructions=(
            "You are Insight-Link Pro — an AI assistant that eliminates hallucinations "
            "by grounding every answer in live repository context and real-time documentation.\n\n"
            "## 3-Stage Pipeline\n"
            "1. **Exploration** — use `map_repository` to understand the codebase layout.\n"
            "2. **Ingestion**   — use `inspect_code`, `web_to_markdown`, or `search_stack_overflow` "
            "to pull relevant source code and official docs.\n"
            "3. **Synthesis**   — produce a grounded, citation-backed answer using the fetched context.\n\n"
            "Always cite the file path or URL you used. Never guess — fetch first, then answer."
        ),
    )

    # Register all tool groups
    register_repo_tools(mcp)
    register_doc_tools(mcp)
    register_analysis_tools(mcp)
    register_session_tools(mcp)

    logger.info(
        " Insight-Link Pro server '%s' initialised with %d tools.",
        config.server_name,
        _TOOL_COUNT,
    )

    # Surface credential warnings at startup
    for warning in config.validate():
        logger.warning("⚠️  %s", warning)

    return mcp


# ------------------------------------------------------------------ #
# CLI
# ------------------------------------------------------------------ #

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Insight-Link Pro MCP Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="MCP transport (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="SSE host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="SSE port (default: 8000)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    mcp = create_server()

    if args.transport == "sse":
        logger.info(" Starting SSE server on %s:%d", args.host, args.port)
        mcp.run(transport="sse", host=args.host, port=args.port)
    else:
        logger.info(" Starting stdio MCP server.")
        mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Server stopped by user.")
        sys.exit(0)
