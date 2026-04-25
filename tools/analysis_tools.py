"""
Repo Intelligence / Analysis Tools
=====================================
MCP tools for deep repository analysis.

Tools:
    - analyze_issues      : Fetch and categorise GitHub issues.
    - dependency_checker  : Check requirements.txt / package.json for outdated
                            or vulnerable packages via PyPI, npm, and OSV.dev.
"""

import json
import logging
import re
from pathlib import Path

import httpx

from core.config import config
from core.context_manager import ContextManager, cache
from fastmcp import FastMCP
from utils.helpers import async_retry, build_error_response, github_limiter

logger = logging.getLogger(__name__)


def register_analysis_tools(mcp: FastMCP) -> None:
    """Register all analysis tools on the given FastMCP instance."""

    @mcp.tool()
    async def analyze_issues(github_repo: str, max_issues: int = 50) -> str:
        """
        Fetch open GitHub issues for a repository and categorise them.

        Categories produced:
            - 🐛 Bugs
            - ✨ Feature requests
            - 🌱 Good first issues
            - 🔒 Security
            - 📖 Documentation
            - 🔧 Other

        Args:
            github_repo: Repository in `owner/repo` format (e.g. `fastapi/fastapi`).
            max_issues:  Maximum issues to analyse (1–100). Default 50.

        Returns:
            Categorised issue breakdown with titles, numbers, and labels.
        """
        repo = github_repo.strip().strip("/")
        if not re.match(r"^[\w.\-]+/[\w.\-]+$", repo):
            return "❌ Invalid repo format. Use `owner/repo` (e.g. `fastapi/fastapi`)."

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
                return f"❌ Repository `{repo}` not found or is private."
            if exc.response.status_code == 403:
                return "❌ GitHub rate limit hit. Set GITHUB_TOKEN in .env to increase quota."
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
            - Python  : `requirements.txt`  (checks PyPI + OSV.dev)
            - Node.js : `package.json`       (checks npm registry + OSV.dev)

        Args:
            manifest_path: Path to the manifest file (absolute or relative).

        Returns:
            Markdown report listing outdated versions and CVE advisories.
        """
        path = Path(manifest_path).expanduser().resolve()
        if not path.exists():
            return f"❌ File not found: `{path}`"

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
                        f"⚠️  Unsupported manifest: `{path.name}`.\n"
                        "Currently supported: `requirements.txt`, `package.json`."
                    )

            await cache.set(cache_key, result)
            return result

        except Exception as exc:
            logger.exception("dependency_checker unexpected error")
            return build_error_response("dependency_checker", exc)


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

    # Label takes priority
    for label in labels:
        for category, keywords in _CATEGORY_KEYWORDS.items():
            if any(kw in label for kw in keywords):
                return category

    # Fall back to title scan
    for category, keywords in _CATEGORY_KEYWORDS.items():
        if any(kw in title_lower for kw in keywords):
            return category

    return "other"


@async_retry(max_attempts=3, backoff=2.0, exceptions=(httpx.RequestError,))
async def _fetch_and_categorise_issues(
    ctx: ContextManager, repo: str, max_issues: int
) -> str:
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
        return f"✅ No open issues found in `{repo}`."

    # Categorise
    categories: dict[str, list[dict]] = {
        "bug": [], "feature": [], "good_first_issue": [],
        "security": [], "documentation": [], "other": [],
    }
    for issue in issues:
        if "pull_request" in issue:
            continue  # skip PRs returned by the issues endpoint
        cat = _categorise_issue(issue)
        categories[cat].append(issue)

    # Render
    emoji_map = {
        "bug": "🐛 Bugs",
        "feature": "✨ Feature Requests",
        "good_first_issue": "🌱 Good First Issues",
        "security": "🔒 Security",
        "documentation": "📖 Documentation",
        "other": "🔧 Other",
    }

    lines = [
        f"# Issue Analysis: `{repo}`\n",
        f"_Analysed {len(issues)} open issues_\n",
    ]

    for cat_key, label in emoji_map.items():
        items = categories[cat_key]
        if not items:
            continue
        lines.append(f"\n## {label} ({len(items)})\n")
        for issue in items[:20]:  # cap per category for readability
            number = issue.get("number", "?")
            title = issue.get("title", "Untitled")
            issue_url = issue.get("html_url", "")
            label_names = ", ".join(lbl["name"] for lbl in issue.get("labels", []))
            label_str = f" `[{label_names}]`" if label_names else ""
            lines.append(f"- [#{number}]({issue_url}) {title}{label_str}")

    return "\n".join(lines)


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
        # Handle ==, >=, <=, ~=, !=
        match = re.match(r"^([A-Za-z0-9_.\-]+)\s*(?:==\s*([^\s,;]+))?", line)
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
            # Strip range specifiers: ^1.2.3 → 1.2.3
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


async def _check_python_deps(ctx: ContextManager, path: Path) -> str:
    """Full Python dependency audit."""
    content = path.read_text(encoding="utf-8")
    packages = _parse_requirements_txt(content)

    if not packages:
        return f"⚠️  No parseable dependencies found in `{path}`."

    lines = [
        f"# Python Dependency Report: `{path.name}`\n",
        f"_Checking {len(packages)} package(s) against PyPI and OSV.dev…_\n",
        "\n| Package | Pinned | Latest | Status | Vulnerabilities |",
        "|---------|--------|--------|--------|-----------------|",
    ]

    outdated_count = vuln_count = ok_count = 0

    for pkg, pinned in packages:
        latest = await _get_pypi_latest(ctx, pkg)
        vulns = await _check_osv_vulnerabilities(ctx, pkg, pinned, "PyPI")

        if latest == "unknown":
            status = "⚠️ Unknown"
        elif not pinned:
            status = "❔ Unpinned"
            ok_count += 1
        elif pinned == latest:
            status = "✅ Up-to-date"
            ok_count += 1
        else:
            status = f"🔄 Outdated → {latest}"
            outdated_count += 1

        vuln_ids = ", ".join(v.get("id", "?") for v in vulns[:3]) if vulns else "—"
        if vulns:
            vuln_count += 1

        lines.append(
            f"| `{pkg}` | `{pinned or '(any)'}` | `{latest}` | {status} | {vuln_ids} |"
        )

    lines.append(f"\n**Summary:** ✅ {ok_count} OK · 🔄 {outdated_count} outdated · 🔒 {vuln_count} vulnerable")
    return "\n".join(lines)


async def _check_node_deps(ctx: ContextManager, path: Path) -> str:
    """Full Node.js dependency audit."""
    content = path.read_text(encoding="utf-8")
    packages = _parse_package_json(content)

    if not packages:
        return f"⚠️  No parseable dependencies found in `{path}`."

    lines = [
        f"# Node.js Dependency Report: `{path.name}`\n",
        f"_Checking {len(packages)} package(s) against npm and OSV.dev…_\n",
        "\n| Package | Pinned | Latest | Status | Vulnerabilities |",
        "|---------|--------|--------|--------|-----------------|",
    ]

    outdated_count = vuln_count = ok_count = 0

    for pkg, pinned in packages:
        latest = await _get_npm_latest(ctx, pkg)
        vulns = await _check_osv_vulnerabilities(ctx, pkg, pinned, "npm")

        if latest == "unknown":
            status = "⚠️ Unknown"
        elif not pinned:
            status = "❔ Unpinned"
            ok_count += 1
        elif pinned == latest:
            status = "✅ Up-to-date"
            ok_count += 1
        else:
            status = f"🔄 Outdated → {latest}"
            outdated_count += 1

        vuln_ids = ", ".join(v.get("id", "?") for v in vulns[:3]) if vulns else "—"
        if vulns:
            vuln_count += 1

        lines.append(
            f"| `{pkg}` | `{pinned or '(any)'}` | `{latest}` | {status} | {vuln_ids} |"
        )

    lines.append(f"\n**Summary:** ✅ {ok_count} OK · 🔄 {outdated_count} outdated · 🔒 {vuln_count} vulnerable")
    return "\n".join(lines)