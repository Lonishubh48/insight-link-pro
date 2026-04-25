"""
Repository Navigation Tools
============================
MCP tools for exploring and reading local codebases.

Tools:
    - map_repository   : Walk the directory tree and return a filtered file tree.
    - inspect_code     : Read a specific line range from a file.
"""

import logging
import os
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from core.config import config
from core.context_manager import cache
from utils.helpers import build_error_response, format_file_tree

logger = logging.getLogger(__name__)

# Directories / files to exclude from the tree
_IGNORE_DIRS: frozenset[str] = frozenset({
    "node_modules", ".git", "dist", "build", "__pycache__",
    ".venv", "venv", ".mypy_cache", ".pytest_cache", "coverage",
    ".tox", ".eggs", "*.egg-info",
})

_IGNORE_EXTENSIONS: frozenset[str] = frozenset({
    ".pyc", ".pyo", ".pyd", ".so", ".dll", ".class",
    ".lock", ".log", ".DS_Store",
})

_MAX_TREE_FILES = 300  # guard against enormous repos


def register_repo_tools(mcp: FastMCP) -> None:
    """Register all repository tools on the given FastMCP instance."""

    @mcp.tool()
    async def map_repository(root_path: str) -> str:
        """
        Walk a local repository and return a filtered ASCII file tree.

        Ignores common noise directories (node_modules, .git, __pycache__, etc.)
        and binary file extensions.

        Args:
            root_path: Absolute or relative path to the repository root.

        Returns:
            A formatted file tree string, or an error message.
        """
        cache_key = f"tree:{root_path}"
        cached = await cache.get(cache_key)
        if cached:
            logger.debug("map_repository cache hit: %s", root_path)
            return cached  # type: ignore[return-value]

        try:
            root = Path(root_path).expanduser().resolve()
            if not root.exists():
                return f"❌ Path not found: `{root}`"
            if not root.is_dir():
                return f"❌ Not a directory: `{root}`"

            tree = _walk_tree(root, root, file_count=0)[0]
            rendered = (
                f"## Repository Map: `{root.name}`\n\n"
                + format_file_tree(tree)
                + f"\n\n_Root: `{root}`_"
            )
            await cache.set(cache_key, rendered)
            return rendered

        except PermissionError as exc:
            return build_error_response("map_repository", exc)
        except Exception as exc:
            logger.exception("map_repository unexpected error")
            return build_error_response("map_repository", exc)

    # ------------------------------------------------------------------ #

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
            start_line: First line to read (1-indexed, inclusive). Default 1.
            end_line:   Last line to read (inclusive). 0 means read to EOF
                        (capped by MAX_FILE_LINES from config).

        Returns:
            The requested code snippet with line numbers, or an error message.
        """
        try:
            path = Path(file_path).expanduser().resolve()
            if not path.exists():
                return f"❌ File not found: `{path}`"
            if not path.is_file():
                return f"❌ Not a regular file: `{path}`"

            # Extension guard
            if path.suffix in _IGNORE_EXTENSIONS:
                return f"⚠️  Skipping binary/compiled file: `{path.suffix}`"

            all_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            total = len(all_lines)

            # Normalise indices (1-based → 0-based)
            start_idx = max(0, start_line - 1)
            end_idx = (total if end_line <= 0 else min(end_line, total))

            # Apply max-line cap
            max_window = config.max_file_lines
            if (end_idx - start_idx) > max_window:
                end_idx = start_idx + max_window
                cap_notice = (
                    f"\n\n⚠️  Output capped at {max_window} lines. "
                    f"Request a smaller range or increase MAX_FILE_LINES."
                )
            else:
                cap_notice = ""

            snippet_lines = all_lines[start_idx:end_idx]
            numbered = "\n".join(
                f"{start_line + i:>6} │ {line}"
                for i, line in enumerate(snippet_lines)
            )

            header = (
                f"## `{path.name}` — lines {start_line}–{start_idx + len(snippet_lines)} "
                f"(total {total} lines)\n\n"
            )
            lang = _ext_to_lang(path.suffix)
            body = f"```{lang}\n{numbered}\n```"

            return header + body + cap_notice

        except UnicodeDecodeError as exc:
            return f"❌ Cannot decode file as UTF-8: {exc}"
        except Exception as exc:
            logger.exception("inspect_code unexpected error")
            return build_error_response("inspect_code", exc)


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def _walk_tree(
    current: Path,
    root: Path,
    file_count: int,
) -> tuple[dict[str, Any], int]:
    """Recursively build a nested dict representing the directory tree."""
    tree: dict[str, Any] = {}

    try:
        entries = sorted(current.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except PermissionError:
        return tree, file_count

    for entry in entries:
        if file_count >= _MAX_TREE_FILES:
            tree["… (truncated)"] = None
            break

        name = entry.name

        if entry.is_dir():
            if name in _IGNORE_DIRS or name.startswith("."):
                continue
            subtree, file_count = _walk_tree(entry, root, file_count)
            if subtree:
                tree[name] = subtree
        elif entry.is_file():
            if entry.suffix in _IGNORE_EXTENSIONS:
                continue
            tree[name] = None
            file_count += 1

    return tree, file_count


_LANG_MAP: dict[str, str] = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".jsx": "jsx", ".tsx": "tsx", ".java": "java", ".go": "go",
    ".rs": "rust", ".c": "c", ".cpp": "cpp", ".h": "c",
    ".cs": "csharp", ".rb": "ruby", ".php": "php",
    ".sh": "bash", ".yml": "yaml", ".yaml": "yaml",
    ".json": "json", ".toml": "toml", ".md": "markdown",
    ".html": "html", ".css": "css", ".sql": "sql",
}


def _ext_to_lang(ext: str) -> str:
    return _LANG_MAP.get(ext.lower(), "text")