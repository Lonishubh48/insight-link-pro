"""
Documentation Tools
====================
MCP tools for fetching and converting external documentation.

Tools:
    - web_to_markdown        : Fetch a URL and return clean Markdown via Jina AI.
    - search_stack_overflow  : Query Stack Exchange API and summarize top answers.
"""

import html
import logging
import re
from urllib.parse import quote_plus

import httpx

from core.config import config
from core.context_manager import ContextManager, cache
from fastmcp import FastMCP
from utils.helpers import async_retry, build_error_response, jina_limiter, se_limiter

logger = logging.getLogger(__name__)


def register_doc_tools(mcp: FastMCP) -> None:
    """Register all documentation tools on the given FastMCP instance."""

    @mcp.tool()
    async def web_to_markdown(url: str) -> str:
        """
        Fetch a web page and convert it to clean Markdown using the Jina AI reader API.

        Ideal for fetching official documentation pages, blog posts, and release notes
        to ground LLM answers in real, up-to-date content.

        Args:
            url: The fully-qualified URL to fetch (must include https://).

        Returns:
            Markdown representation of the page content, or an error message.
        """
        if not url.startswith(("http://", "https://")):
            return "❌ Invalid URL. Must start with `http://` or `https://`."

        cache_key = f"web:{url}"
        cached = await cache.get(cache_key)
        if cached:
            logger.debug("web_to_markdown cache hit: %s", url)
            return cached  # type: ignore[return-value]

        await jina_limiter.acquire()

        try:
            async with ContextManager() as ctx:
                result = await _fetch_via_jina(ctx, url)

            await cache.set(cache_key, result)
            return result

        except httpx.TimeoutException:
            return f"❌ Request timed out after {config.request_timeout}s for URL: `{url}`"
        except httpx.HTTPStatusError as exc:
            return f"❌ HTTP {exc.response.status_code} fetching `{url}`: {exc.response.text[:300]}"
        except Exception as exc:
            logger.exception("web_to_markdown unexpected error")
            return build_error_response("web_to_markdown", exc)

    # ------------------------------------------------------------------ #

    @mcp.tool()
    async def search_stack_overflow(query: str, num_results: int = 5) -> str:
        """
        Search Stack Overflow (via Stack Exchange API) and return summarized answers.

        Fetches the top questions matching the query and includes the accepted (or
        highest-voted) answer for each, giving LLMs verified community solutions.

        Args:
            query:       The search query string (e.g. "async context manager Python").
            num_results: Number of questions to fetch (1–10). Default 5.

        Returns:
            Formatted Markdown with questions and their best answers.
        """
        if not query.strip():
            return "❌ Query must not be empty."

        num_results = max(1, min(num_results, 10))
        cache_key = f"so:{query}:{num_results}"
        cached = await cache.get(cache_key)
        if cached:
            logger.debug("search_stack_overflow cache hit: %s", query)
            return cached  # type: ignore[return-value]

        await se_limiter.acquire()

        try:
            async with ContextManager() as ctx:
                result = await _query_stack_exchange(ctx, query, num_results)

            await cache.set(cache_key, result)
            return result

        except httpx.TimeoutException:
            return f"❌ Stack Overflow API timed out for query: `{query}`"
        except Exception as exc:
            logger.exception("search_stack_overflow unexpected error")
            return build_error_response("search_stack_overflow", exc)


# ------------------------------------------------------------------ #
# Private helpers
# ------------------------------------------------------------------ #

@async_retry(max_attempts=3, backoff=2.0, exceptions=(httpx.RequestError,))
async def _fetch_via_jina(ctx: ContextManager, url: str) -> str:
    """Call Jina reader and return markdown content."""
    jina_url = f"{config.jina_base_url}/{url}"
    headers: dict[str, str] = {"Accept": "text/markdown,text/plain"}
    if config.jina_api_key:
        headers["Authorization"] = f"Bearer {config.jina_api_key}"

    resp = ctx.http_client.build_request("GET", jina_url, headers=headers)
    response = await ctx.http_client.send(resp)
    response.raise_for_status()

    markdown = response.text
    if not markdown.strip():
        return f"⚠️  Jina returned empty content for `{url}`. The page may require JavaScript."

    truncated = ContextManager.truncate(markdown, label=f"web_to_markdown({url})")
    return f"## Source: [{url}]({url})\n\n{truncated}"


@async_retry(max_attempts=3, backoff=2.0, exceptions=(httpx.RequestError,))
async def _query_stack_exchange(
    ctx: ContextManager, query: str, num_results: int
) -> str:
    """Hit the Stack Exchange search API and format results."""
    params: dict[str, str | int] = {
        "order": "desc",
        "sort": "relevance",
        "intitle": query,
        "site": "stackoverflow",
        "filter": "withbody",
        "pagesize": num_results,
        "page": 1,
    }
    if config.se_api_key:
        params["key"] = config.se_api_key

    search_resp = await ctx.http_client.get(
        f"{config.se_api_base}/search/advanced",
        params=params,  # type: ignore[arg-type]
    )
    search_resp.raise_for_status()
    data = search_resp.json()

    items = data.get("items", [])
    if not items:
        return f"🔍 No Stack Overflow results found for: **{query}**"

    sections: list[str] = [
        f"# Stack Overflow: `{query}`\n",
        f"_{len(items)} result(s) found_\n",
    ]

    for idx, item in enumerate(items, 1):
        title = html.unescape(item.get("title", "Untitled"))
        link = item.get("link", "")
        score = item.get("score", 0)
        answer_count = item.get("answer_count", 0)
        is_answered = item.get("is_answered", False)
        answered_badge = "✅" if is_answered else "❓"

        sections.append(
            f"\n---\n"
            f"### {idx}. {answered_badge} [{title}]({link})\n"
            f"**Score:** {score} | **Answers:** {answer_count}\n"
        )

        # Include body of the question (lightly cleaned)
        q_body = _clean_html(item.get("body", ""))
        if q_body:
            sections.append(f"**Question excerpt:**\n{q_body[:600]}\n")

        # Fetch the accepted / top answer for answered questions
        if is_answered and answer_count > 0:
            q_id = item.get("question_id")
            answer_text = await _fetch_top_answer(ctx, q_id)
            if answer_text:
                sections.append(f"**Best Answer:**\n{answer_text[:800]}\n")

    result = "\n".join(sections)
    return ContextManager.truncate(result, "search_stack_overflow")


async def _fetch_top_answer(ctx: ContextManager, question_id: int) -> str:
    """Fetch and clean the top-voted answer for a question."""
    try:
        params: dict[str, str | int] = {
            "order": "desc",
            "sort": "votes",
            "site": "stackoverflow",
            "filter": "withbody",
            "pagesize": 1,
        }
        if config.se_api_key:
            params["key"] = config.se_api_key

        resp = await ctx.http_client.get(
            f"{config.se_api_base}/questions/{question_id}/answers",
            params=params,  # type: ignore[arg-type]
        )
        resp.raise_for_status()
        answers = resp.json().get("items", [])
        if answers:
            return _clean_html(answers[0].get("body", ""))
    except Exception as exc:
        logger.debug("Could not fetch answer for Q%d: %s", question_id, exc)
    return ""


def _clean_html(raw: str) -> str:
    """Strip HTML tags and unescape entities for readable text."""
    raw = html.unescape(raw)
    # Remove code blocks and replace with placeholder to avoid noise
    raw = re.sub(r"<pre[^>]*>.*?</pre>", "\n[code block omitted]\n", raw, flags=re.DOTALL)
    # Strip all remaining tags
    raw = re.sub(r"<[^>]+>", "", raw)
    # Normalise whitespace
    raw = re.sub(r"\n{3,}", "\n\n", raw).strip()
    return raw