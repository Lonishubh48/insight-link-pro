"""
Repository Navigation Tools
============================
MCP tools for exploring and reading local codebases.
Tools:
    - map_repository   : Walk the directory tree and return a filtered file tree.
    - inspect_code     : Read a specific line range from a file.

HOW THIS FILE FITS IN THE PROJECT:
    main.py → calls register_repo_tools(mcp)
            → which registers map_repository and inspect_code as MCP tools
            → Claude Desktop can then call these tools during a conversation

"""

import logging
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from core.config import config
from core.context_manager import cache
from utils.helpers import build_error_response, format_file_tree

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# CONSTANTS — Directories and file types to always skip
#
# WHY: Large repos have thousands of files in node_modules, __pycache__
# etc. that are irrelevant to understanding code. Skipping them keeps
# the file tree clean and prevents hitting _MAX_TREE_FILES too early.
#
# NOTE: All dotfiles/dotdirs (e.g. .git, .venv, .mypy_cache) are ALSO
# skipped via the `name.startswith(".")` check in _walk_tree.
# ------------------------------------------------------------------ #

# Standardized Ignore Constants
_IGNORE_DIRS: frozenset[str] = frozenset({
    "node_modules", "dist", "build", "__pycache__",
    "venv", "coverage", ".tox", ".eggs", "*.egg-info",
})

_IGNORE_EXTENSIONS: frozenset[str] = frozenset({
    ".pyc", ".pyo", ".pyd", ".so", ".dll", ".class",
    ".lock", ".log", ".DS_Store",
})

# Safety cap: stop walking after this many files to prevent
# memory issues on enormous monorepos without proper .mcpignore in place.

_MAX_TREE_FILES = 300


# ------------------------------------------------------------------ #
# TOOL REGISTRATION
#
# register_repo_tools() is called once at startup from main.py.
# It receives the FastMCP instance and attaches tools to it using
# the @mcp.tool() decorator pattern.
#
# WHY use a registration function instead of global decorators?
# → Keeps tools modular and testable in isolation
# → Allows multiple tool groups to be registered on the same server
# ------------------------------------------------------------------ #

def register_repo_tools(mcp: FastMCP) -> None:
    """Register all repository tools on the given FastMCP instance."""

    @mcp.tool()
    async def map_repository(root_path: str) -> str:
        """
    Walk a local repository and return a filtered ASCII file tree.

    Ignores common noise directories (node_modules, .git, __pycache__, etc.)
    and binary file extensions. Respects .mcpignore for custom exclusions.

    Args:
        root_path: Absolute or relative path to the repository root.
                   Examples: "F:/myproject", ".", "/home/user/repos/myapp"

    Returns:
        A formatted file tree string, or an error message if path is invalid.
    """
        # Cache key is based on the raw path string.
        # If the same path is requested again within TTL (default 5 min),
        # we return the cached result instead of re-walking the filesystem.
        # This is especially beneficial for large repos where walking can be expensive.        
        cache_key = f"tree:{root_path}"
        cached = await cache.get(cache_key)
        if cached:
            logger.debug("map_repository cache hit: %s", root_path) 
            return cached

        try:
            root = Path(root_path).expanduser().resolve()

            if not root.exists():
                return f"ERROR: Path not found: '{root}'"
            if not root.is_dir():
                return f"ERROR: Path is not a directory: '{root}'"
            
            # _walk_tree() returns a nested dict like:
            # {"core": {"config.py": None, "context_manager.py": None}, "main.py": None}
            # file_count=0 starts the counter fresh

            tree_data, _ = _walk_tree(root, root, file_count=0)

            output = [
                f"### Repository Map: {root.name}",
                format_file_tree(tree_data),
                f"\nLocation: {root}"
            ]
            
            rendered = "\n".join(output)
            await cache.set(cache_key, rendered)
            return rendered

        except PermissionError as exc:
            return build_error_response("map_repository", exc)
        except Exception as exc:
            logger.exception("map_repository unexpected error")
            return build_error_response("map_repository", exc)

    @mcp.tool()
    async def inspect_code(
        file_path: str,
        start_line: int = 1,
        end_line: int = 0,
    ) -> str:
        """
        Read a specific line range from a source file.

        Args:
            file_path:  Absolute or relative path to the file.
                        Example: "F:/myproject/src/main.py"
            start_line: First line to read (1-indexed, inclusive). Default 1.
            end_line:   Last line to read (inclusive).
                        0 = read to EOF (capped by MAX_FILE_LINES from .env).
                        Example: start_line=50, end_line=100 reads lines 50-100.

        Returns:
            The requested code snippet with line numbers, or an error message.
        """
        try:
            path = Path(file_path).expanduser().resolve()

            if not path.exists():
                return f"ERROR: File not found: '{path}'"
            if not path.is_file():
                return f"ERROR: Path is not a regular file: '{path}'"

            if path.suffix in _IGNORE_EXTENSIONS:
                return f"SKIP: Binary or compiled file type detected: '{path.suffix}'"

            all_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            total = len(all_lines)

            start_idx = max(0, start_line - 1)
            end_idx = (total if end_line <= 0 else min(end_line, total))

            # Apply safety cap
            max_window = config.max_file_lines
            limit_reached = False
            if (end_idx - start_idx) > max_window:
                end_idx = start_idx + max_window
                limit_reached = True

            snippet_lines = all_lines[start_idx:end_idx]
            
            # Formatted code block with line indicators
            numbered = "\n".join(
                f"{start_line + i:>6} | {line}"
                for i, line in enumerate(snippet_lines)
            )

            lang = _ext_to_lang(path.suffix)
            header = f"### File: {path.name} (Lines {start_line}-{start_line + len(snippet_lines) - 1} of {total})\n"
            body = f"```{lang}\n{numbered}\n```"
            
            footer = ""
            if limit_reached:
                footer = f"\n\nNOTE: Output truncated to {max_window} lines per configuration."

            return header + body + footer

        except UnicodeDecodeError as exc:
            return f"ERROR: Failed to decode file as UTF-8: {exc}"
        except Exception as exc:
            logger.exception("inspect_code unexpected error")
            return build_error_response("inspect_code", exc)

# ------------------------------------------------------------------ #
# Internal Helpers
# ------------------------------------------------------------------ #

def _load_mcpignore(root: Path) -> frozenset[str]:
    """
    Load custom ignore patterns from a .mcpignore file in the repo root.

    Works exactly like .gitignore — one pattern per line, # = comment.

    WHY: Enterprises need a way to tell the MCP server "don't scan
    these directories" — e.g. secrets/, internal-config/, etc.
    This gives users control over what data the server can access.

    Example .mcpignore file:
        # Sensitive directories
        secrets/
        .env*
        internal-config/
        *.pem
        *.key

    Args:
        root: The repository root Path to look for .mcpignore in.

    Returns:
        A frozenset of pattern strings, or empty frozenset if no .mcpignore exists.
    """
    ignore_file = root / ".mcpignore"
    if not ignore_file.exists():
        return frozenset()

    patterns: set[str] = set()
    try:
        for line in ignore_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.add(line)
    except OSError as exc:
        logger.warning("Could not read .mcpignore at %s: %s", ignore_file, exc)
        return frozenset()

    return frozenset(patterns)

def _is_ignored(name: str, patterns: frozenset[str]) -> bool:
    """Evaluate name against ignore patterns."""
    return any(fnmatch(name, pattern) for pattern in patterns)

def _walk_tree(
    current: Path,
    root: Path,
    file_count: int,
    mcpignore_patterns: frozenset[str] | None = None,
) -> tuple[dict[str, Any], int]:
    """Recursive directory traversal logic."""
    if mcpignore_patterns is None:
        mcpignore_patterns = _load_mcpignore(root)

    tree: dict[str, Any] = {}

    try:
        entries = sorted(current.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except PermissionError:
        return tree, file_count

    for entry in entries:
        if file_count >= _MAX_TREE_FILES:
            tree["[TRUNCATED]"] = None
            break

        name = entry.name

        if _is_ignored(name, mcpignore_patterns):
            continue

        if entry.is_dir():
            if name in _IGNORE_DIRS or name.startswith("."):
                continue

            subtree, file_count = _walk_tree(entry, root, file_count, mcpignore_patterns)
            if subtree:
                tree[name] = subtree

        elif entry.is_file():
            if entry.suffix in _IGNORE_EXTENSIONS:
                continue

            tree[name] = None
            file_count += 1

    return tree, file_count

_LANG_MAP: dict[str, str] = {
    ".py": "python",    ".js": "javascript",  ".ts": "typescript",
    ".jsx": "jsx",      ".tsx": "tsx",        ".java": "java",
    ".go": "go",        ".rs": "rust",        ".c": "c",
    ".cpp": "cpp",      ".h": "c",            ".cs": "csharp",
    ".rb": "ruby",      ".php": "php",        ".sh": "bash",
    ".yml": "yaml",     ".yaml": "yaml",      ".json": "json",
    ".toml": "toml",    ".md": "markdown",    ".html": "html",
    ".css": "css",      ".sql": "sql",
}

def _ext_to_lang(ext: str) -> str:
    """Map extension to Markdown language identifier."""
    return _LANG_MAP.get(ext.lower(), "text")