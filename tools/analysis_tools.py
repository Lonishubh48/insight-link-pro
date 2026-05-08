"""
Repo Intelligence / Analysis Tools
=====================================
MCP tools for deep repository analysis.

Tools:
    - analyze_issues      : Fetch and categorise GitHub issues.
    - dependency_checker  : Check requirements.txt / package.json for outdated
                            or vulnerable packages via PyPI, npm, and OSV.dev.
    - map_github_repo     : Fetch the complete file tree of any public GitHub
                            repository without cloning.
"""

import json
import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from core.config import config
from core.context_manager import ContextManager, cache
from core.session_store import session_store
from fastmcp import FastMCP
from utils.helpers import async_retry, build_error_response, format_file_tree, github_limiter

logger = logging.getLogger(__name__)


def register_analysis_tools(mcp: FastMCP) -> None:
    """Register all analysis tools on the given FastMCP instance."""

    @mcp.tool()
    async def analyze_issues(github_repo: str, max_issues: int = 50) -> str:
        """
        Fetch open GitHub issues for a repository and categorise them.

        Categories produced:
            - Bugs
            - Feature requests
            - Good first issues
            - Security
            - Documentation
            - Other

        Args:
            github_repo: Repository in owner/repo format (e.g. fastapi/fastapi).
            max_issues:  Maximum issues to analyse (1-100). Default 50.

        Returns:
            Categorised issue breakdown with titles, numbers, and labels.
        """
        repo = github_repo.strip().strip("/")
        if not re.match(r"^[\w.\-]+/[\w.\-]+$", repo):
            return "ERROR: Invalid repo format. Use owner/repo (e.g. fastapi/fastapi)."

        max_issues = max(1, min(max_issues, 100))
        cache_key = f"issues:{repo}:{max_issues}"
        cached = await cache.get(cache_key)
        if cached:
            logger.debug("analyze_issues cache hit: %s", repo)
            return cached  # type: ignore[return-value]

        await github_limiter.acquire()

        try:
            async with ContextManager() as ctx:
                result = await _fetch_and_categorise_issues(ctx, repo, max_issues)

            await cache.set(cache_key, result)
            return result

        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return f"ERROR: Repository '{repo}' not found or is private."
            if exc.response.status_code == 403:
                return "ERROR: GitHub rate limit hit. Set GITHUB_TOKEN in .env to increase quota."
            return build_error_response("analyze_issues", exc)
        except Exception as exc:
            logger.exception("analyze_issues unexpected error")
            return build_error_response("analyze_issues", exc)

    # ------------------------------------------------------------------ #

    @mcp.tool()
    async def dependency_checker(manifest_path: str) -> str:
        """
        Check a dependency manifest for outdated and known-vulnerable packages.

        Supports:
            - Python  : requirements.txt  (checks PyPI + OSV.dev)
            - Node.js : package.json      (checks npm registry + OSV.dev)

        Args:
            manifest_path: Path to the manifest file (absolute or relative).

        Returns:
            Markdown report listing outdated versions and CVE advisories.
        """
        path = Path(manifest_path).expanduser().resolve()
        if not path.exists():
            return f"ERROR: File not found: '{path}'"

        cache_key = f"deps:{path}"
        cached = await cache.get(cache_key)
        if cached:
            logger.debug("dependency_checker cache hit: %s", path)
            return cached  # type: ignore[return-value]

        try:
            async with ContextManager() as ctx:
                if path.name == "requirements.txt":
                    result = await _check_python_deps(ctx, path)
                elif path.name == "package.json":
                    result = await _check_node_deps(ctx, path)
                else:
                    return (
                        f"ERROR: Unsupported manifest: '{path.name}'.\n"
                        "Currently supported: requirements.txt, package.json."
                    )

            await cache.set(cache_key, result)
            return result

        except Exception as exc:
            logger.exception("dependency_checker unexpected error")
            return build_error_response("dependency_checker", exc)

    # ------------------------------------------------------------------ #

    @mcp.tool()
    async def map_github_repo(github_repo: str) -> str:
        """
        Fetch the complete file tree of any public GitHub repository
        using the GitHub Trees API — no cloning required.

        Works identically to map_repository but for remote public repos.
        Use this as Stage 1 (Exploration) when working with any public
        GitHub repository without needing to clone it locally.

        After getting the tree, use web_to_markdown with the raw GitHub URL
        to read any specific file:
            https://raw.githubusercontent.com/owner/repo/branch/path/to/file.py

        Args:
            github_repo: Repository in owner/repo format.
                         Examples: "fastapi/fastapi",
                                   "Vishnu-Naik/moire_pattern_detector"

        Returns:
            Formatted file tree string with instructions for reading individual
            files via web_to_markdown. Returns an error message if the repo
            is not found or is private.
        """
        repo = github_repo.strip().strip("/")
        if not re.match(r"^[\w.\-]+/[\w.\-]+$", repo):
            return "ERROR: Invalid repo format. Use owner/repo (e.g. fastapi/fastapi)."

        cache_key = f"github_tree:{repo}"
        cached = await cache.get(cache_key)
        if cached:
            logger.debug("map_github_repo cache hit: %s", repo)
            return cached  # type: ignore[return-value]

        await github_limiter.acquire()

        try:
            async with ContextManager() as ctx:
                result = await _fetch_github_tree(ctx, repo)

            await cache.set(cache_key, result)
            await session_store.update_repo_scan(
                repo_path=f"github:{repo}",
                tree_summary=result,
            )
            return result

        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return f"ERROR: Repository '{repo}' not found or is private."
            if exc.response.status_code == 403:
                return "ERROR: GitHub rate limit hit. Set GITHUB_TOKEN in .env to increase quota."
            return build_error_response("map_github_repo", exc)
        except Exception as exc:
            logger.exception("map_github_repo unexpected error")
            return build_error_response("map_github_repo", exc)


# ------------------------------------------------------------------ #
# Issue analysis helpers
# ------------------------------------------------------------------ #

_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "bug": ["bug", "error", "crash", "fix", "broken", "fail", "regression", "exception"],
    "feature": ["feature", "enhancement", "request", "add", "support", "implement", "improve"],
    "good_first_issue": ["good first issue", "beginner", "easy", "starter", "help wanted"],
    "security": ["security", "vulnerability", "cve", "exploit", "injection", "auth"],
    "documentation": ["docs", "documentation", "readme", "typo", "example", "guide"],
}


def _categorise_issue(issue: dict) -> str:
    """Map an issue to a category based on labels and title keywords."""
    labels: list[str] = [lbl["name"].lower() for lbl in issue.get("labels", [])]
    title_lower = issue.get("title", "").lower()

    for label in labels:
        for category, keywords in _CATEGORY_KEYWORDS.items():
            if any(kw in label for kw in keywords):
                return category

    for category, keywords in _CATEGORY_KEYWORDS.items():
        if any(kw in title_lower for kw in keywords):
            return category

    return "other"


@async_retry(max_attempts=3, backoff=2.0, exceptions=(httpx.RequestError,))
async def _fetch_and_categorise_issues(
    ctx: ContextManager, repo: str, max_issues: int
) -> str:
    """Call the GitHub Issues API and return a categorised Markdown report."""
    headers = {"Accept": "application/vnd.github+json"}
    if config.github_token:
        headers["Authorization"] = f"Bearer {config.github_token}"

    per_page = min(max_issues, 100)
    url = f"{config.github_api_base}/repos/{repo}/issues"
    params = {"state": "open", "per_page": per_page, "page": 1}

    response = await ctx.http_client.get(url, headers=headers, params=params)
    response.raise_for_status()
    issues = response.json()

    if not issues:
        return f"No open issues found in '{repo}'."

    categories: dict[str, list[dict]] = {
        "bug": [], "feature": [], "good_first_issue": [],
        "security": [], "documentation": [], "other": [],
    }
    for issue in issues:
        if "pull_request" in issue:
            continue
        cat = _categorise_issue(issue)
        categories[cat].append(issue)

    category_labels = {
        "bug": "Bugs",
        "feature": "Feature Requests",
        "good_first_issue": "Good First Issues",
        "security": "Security",
        "documentation": "Documentation",
        "other": "Other",
    }

    lines = [
        f"# Issue Analysis: {repo}\n",
        f"Analysed {len(issues)} open issues\n",
    ]

    for cat_key, label in category_labels.items():
        items = categories[cat_key]
        if not items:
            continue
        lines.append(f"\n## {label} ({len(items)})\n")
        for issue in items[:20]:
            number = issue.get("number", "?")
            title = issue.get("title", "Untitled")
            issue_url = issue.get("html_url", "")
            label_names = ", ".join(lbl["name"] for lbl in issue.get("labels", []))
            label_str = f" [{label_names}]" if label_names else ""
            lines.append(f"- [#{number}]({issue_url}) {title}{label_str}")

    return "\n".join(lines)


# ------------------------------------------------------------------ #
# GitHub tree helper
# ------------------------------------------------------------------ #

_BINARY_EXTENSIONS: tuple[str, ...] = (
    ".pyc", ".pyo", ".so", ".dll", ".class",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg",
    ".zip", ".tar", ".gz", ".whl", ".egg",
    ".mp4", ".mp3", ".mov", ".avi",
    ".pdf", ".docx", ".xlsx",
)


@async_retry(max_attempts=3, backoff=2.0, exceptions=(httpx.RequestError,))
async def _fetch_github_tree(ctx: ContextManager, repo: str) -> str:
    """
    Call the GitHub Trees API and format the result as an ASCII file tree.

    Uses recursive=1 to fetch the complete tree in a single API call.
    Filters out binary and compiled files to keep output focused on source code.

    Args:
        ctx:  Active ContextManager with shared HTTP client.
        repo: Repository in owner/repo format.

    Returns:
        Formatted file tree string with instructions for reading individual files.
    """
    headers = {"Accept": "application/vnd.github+json"}
    if config.github_token:
        headers["Authorization"] = f"Bearer {config.github_token}"

    # Step 1: Get repo metadata including default branch
    repo_resp = await ctx.http_client.get(
        f"{config.github_api_base}/repos/{repo}",
        headers=headers,
    )
    repo_resp.raise_for_status()
    repo_data = repo_resp.json()
    default_branch = repo_data.get("default_branch", "main")
    description = repo_data.get("description", "")
    stars = repo_data.get("stargazers_count", 0)
    language = repo_data.get("language", "Unknown")

    # Step 2: Fetch the full recursive tree in one API call
    tree_resp = await ctx.http_client.get(
        f"{config.github_api_base}/repos/{repo}/git/trees/{default_branch}",
        headers=headers,
        params={"recursive": "1"},
    )
    tree_resp.raise_for_status()
    data = tree_resp.json()

    truncation_notice = (
        "\nNOTE: Tree truncated by GitHub — repository exceeds 100,000 files.\n"
        if data.get("truncated") else ""
    )

    items = data.get("tree", [])
    if not items:
        return f"No files found in repository: {repo}"

    # Step 3: Convert flat path list to nested dict
    tree_dict: dict = {}
    file_count = 0

    for item in sorted(items, key=lambda x: x.get("path", "")):
        path = item.get("path", "")
        item_type = item.get("type", "")

        if any(path.endswith(ext) for ext in _BINARY_EXTENSIONS):
            continue

        if file_count >= 300:
            break

        parts = path.split("/")
        current = tree_dict

        if item_type == "tree":
            for part in parts:
                if part not in current:
                    current[part] = {}
                current = current[part]

        elif item_type == "blob":
            for part in parts[:-1]:
                if part not in current:
                    current[part] = {}
                current = current[part]
            current[parts[-1]] = None
            file_count += 1

    # Step 4: Render the tree with instructions
    raw_base = f"https://raw.githubusercontent.com/{repo}/{default_branch}"

    parts_rendered = [
        f"### GitHub Repository: {repo}",
        f"Branch: {default_branch} | Language: {language} | Stars: {stars}",
    ]
    if description:
        parts_rendered.append(f"Description: {description}")

    parts_rendered += [
        "",
        format_file_tree(tree_dict),
        "",
        f"Files shown: {file_count}{truncation_notice}",
        "",
        "To read any file use web_to_markdown with the raw URL:",
        f"  {raw_base}/PATH_TO_FILE",
        "",
        "Example:",
        f"  web_to_markdown(url='{raw_base}/README.md')",
    ]

    return "\n".join(parts_rendered)


# ------------------------------------------------------------------ #
# Dependency checker helpers
# ------------------------------------------------------------------ #

def _parse_requirements_txt(content: str) -> list[tuple[str, str]]:
    """Parse requirements.txt into (package, pinned_version) pairs."""
    packages: list[tuple[str, str]] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "-", "git+")):
            continue
        match = re.match(
            r"^([A-Za-z0-9_.\-]+)\s*(?:[><=~!]=?\s*([^\s,;]+))?",
            line,
        )
        if match:
            name = match.group(1)
            version = match.group(2) or ""
            packages.append((name, version))
    return packages


def _parse_package_json(content: str) -> list[tuple[str, str]]:
    """Parse package.json into (package, version) pairs from dependencies + devDependencies."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return []
    packages: list[tuple[str, str]] = []
    for section in ("dependencies", "devDependencies"):
        for pkg, ver in data.get(section, {}).items():
            clean_ver = re.sub(r"^[\^~>=<]", "", str(ver)).strip()
            packages.append((pkg, clean_ver))
    return packages


@async_retry(max_attempts=2, backoff=1.5, exceptions=(httpx.RequestError,))
async def _get_pypi_latest(ctx: ContextManager, package: str) -> str:
    """Fetch the latest version of a PyPI package."""
    try:
        resp = await ctx.http_client.get(f"https://pypi.org/pypi/{package}/json")
        if resp.status_code == 200:
            return resp.json()["info"]["version"]
    except Exception as exc:
        logger.debug("PyPI lookup failed for %s: %s", package, exc)
    return "unknown"


@async_retry(max_attempts=2, backoff=1.5, exceptions=(httpx.RequestError,))
async def _get_npm_latest(ctx: ContextManager, package: str) -> str:
    """Fetch the latest version of an npm package."""
    try:
        resp = await ctx.http_client.get(f"https://registry.npmjs.org/{package}/latest")
        if resp.status_code == 200:
            return resp.json().get("version", "unknown")
    except Exception as exc:
        logger.debug("npm lookup failed for %s: %s", package, exc)
    return "unknown"


@async_retry(max_attempts=2, backoff=1.5, exceptions=(httpx.RequestError,))
async def _check_osv_vulnerabilities(
    ctx: ContextManager,
    package: str,
    version: str,
    ecosystem: str,
) -> list[dict]:
    """Query OSV.dev for known vulnerabilities (free, no API key needed)."""
    if not version:
        return []
    try:
        payload = {
            "version": version,
            "package": {"name": package, "ecosystem": ecosystem},
        }
        resp = await ctx.http_client.post(
            f"{config.osv_api_base}/query",
            json=payload,
        )
        if resp.status_code == 200:
            return resp.json().get("vulns", [])
    except Exception as exc:
        logger.debug("OSV lookup failed for %s@%s: %s", package, version, exc)
    return []


async def _build_dep_report(
    ctx: ContextManager,
    path: Path,
    packages: list[tuple[str, str]],
    fetch_latest: Callable[[ContextManager, str], Awaitable[str]],
    ecosystem: str,
    registry_label: str,
) -> str:
    """Shared table-building logic for Python and Node.js dependency audits."""
    lines = [
        f"# {registry_label} Dependency Report: `{path.name}`\n",
        f"Checking {len(packages)} package(s) against {registry_label} and OSV.dev\n",
        "\n| Package | Pinned | Latest | Status | Vulnerabilities |",
        "|---------|--------|--------|--------|-----------------|",
    ]

    outdated_count = vuln_count = ok_count = 0

    for pkg, pinned in packages:
        latest = await fetch_latest(ctx, pkg)
        vulns = await _check_osv_vulnerabilities(ctx, pkg, pinned, ecosystem)

        if latest == "unknown":
            status = "Unknown"
        elif not pinned:
            status = "Unpinned"
            ok_count += 1
        elif pinned == latest:
            status = "Up-to-date"
            ok_count += 1
        else:
            status = f"Outdated -> {latest}"
            outdated_count += 1

        vuln_ids = ", ".join(v.get("id", "?") for v in vulns[:3]) if vulns else "-"
        if vulns:
            vuln_count += 1

        lines.append(
            f"| `{pkg}` | `{pinned or '(any)'}` | `{latest}` | {status} | {vuln_ids} |"
        )

    lines.append(
        f"\n**Summary:** {ok_count} OK / {outdated_count} outdated / {vuln_count} vulnerable"
    )
    return "\n".join(lines)


async def _check_python_deps(ctx: ContextManager, path: Path) -> str:
    """Run a full dependency audit for a Python requirements.txt file."""
    content = path.read_text(encoding="utf-8")
    packages = _parse_requirements_txt(content)
    if not packages:
        return f"ERROR: No parseable dependencies found in '{path}'."
    return await _build_dep_report(ctx, path, packages, _get_pypi_latest, "PyPI", "Python")


async def _check_node_deps(ctx: ContextManager, path: Path) -> str:
    """Run a full dependency audit for a Node.js package.json file."""
    content = path.read_text(encoding="utf-8")
    packages = _parse_package_json(content)
    if not packages:
        return f"ERROR: No parseable dependencies found in '{path}'."
    return await _build_dep_report(ctx, path, packages, _get_npm_latest, "npm", "Node.js")