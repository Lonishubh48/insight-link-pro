# 🔗 Insight-Link Pro

> **The Hallucination Killer** — An MCP server that grounds every LLM answer in live repository context and real-time documentation.

---

## The Problem: LLMs Hallucinate

When you ask an AI to help with your codebase, it guesses. It invents APIs that don't exist, references library versions you're not running, and misreads your architecture. The root cause: **the model has no live access to your actual code or current docs.**

Insight-Link Pro solves this with a **3-stage execution pipeline**:

| Stage | What Happens |
|-------|-------------|
| 🔭 **Exploration** | `map_repository` — understand the structure of your codebase |
| 📥 **Ingestion** | `inspect_code` + `web_to_markdown` + `search_stack_overflow` — pull real context |
| 🧠 **Synthesis** | Grounded, citation-backed answers — zero hallucination |

---

## Demo

```
User: "Why is my FastAPI app returning 422 errors on /users endpoint?"

Insight-Link Pro:
  1. map_repository("/home/user/myapp")
     → Found: app/routes/users.py, app/models/user.py, requirements.txt

  2. inspect_code("app/routes/users.py", start_line=1, end_line=80)
     → Reads the actual route handler

  3. web_to_markdown("https://fastapi.tiangolo.com/tutorial/body/")
     → Fetches live Pydantic/FastAPI body validation docs

  4. search_stack_overflow("FastAPI 422 Unprocessable Entity pydantic validation")
     → Finds the top accepted Stack Overflow answer

Answer: "Line 34 in users.py — your UserCreate model requires `email` as a
  required field (no default), but your test client is sending `user_email`.
  See FastAPI docs §Request Body: the field name must match exactly. Stack
  Overflow answer #71234567 confirms this is the #1 cause of 422s."
```

**No guessing. Every claim is backed by a source you can verify.**

---

## Features

| Tool | Description |
|------|-------------|
| `map_repository` | Filtered file tree (ignores noise dirs) |
| `inspect_code` | Line-range reading with syntax highlighting |
| `web_to_markdown` | Clean Markdown from any URL via Jina AI |
| `search_stack_overflow` | Top SO answers summarised per query |
| `analyze_issues` | GitHub issues categorised by type |
| `dependency_checker` | Outdated + CVE-vulnerable packages via PyPI/npm/OSV |

---

## Setup

### 1. Clone & Install

```bash
git clone https://github.com/yourname/insight-link-pro
cd insight-link-pro
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your API keys
```

**Required for full functionality:**

| Variable | Where to get it | Effect without it |
|----------|----------------|-------------------|
| `GITHUB_TOKEN` | [github.com/settings/tokens](https://github.com/settings/tokens) | 60 req/hr (very limited) |
| `JINA_API_KEY` | [jina.ai](https://jina.ai/) | 20 free req/day |
| `SE_API_KEY` | [stackapps.com](https://stackapps.com/apps/oauth/register) | 300 req/day |

### 3. Run

**stdio (for Claude Desktop / MCP clients):**
```bash
python main.py
```

**SSE HTTP server:**
```bash
python main.py --transport sse --port 8000
```

### 4. Connect to Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "insight-link-pro": {
      "command": "python",
      "args": ["/absolute/path/to/insight_link_pro/main.py"],
      "env": {
        "GITHUB_TOKEN": "ghp_...",
        "JINA_API_KEY": "jina_...",
        "SE_API_KEY": "..."
      }
    }
  }
}
```

---

## Architecture

```
insight_link_pro/
├── main.py                    # FastMCP server assembly + CLI
├── core/
│   ├── config.py              # Typed settings from .env
│   └── context_manager.py     # TTL cache + shared HTTP client + budget control
├── tools/
│   ├── repo_tools.py          # map_repository, inspect_code
│   ├── doc_tools.py           # web_to_markdown, search_stack_overflow
│   └── analysis_tools.py      # analyze_issues, dependency_checker
├── utils/
│   └── helpers.py             # Logging, rate limiter, retry decorator, formatters
├── .env.example
└── requirements.txt
```

### Key Design Decisions

- **All tools are `async`** — no blocking I/O, scales to concurrent MCP requests.
- **TTL cache** — repeat queries hit memory, not the network (configurable TTL).
- **Token budget control** — `MAX_RESPONSE_CHARS` prevents context window explosions.
- **Rate limiters** — per-API sliding-window limiters prevent quota exhaustion.
- **Retry with backoff** — transient network errors recover automatically.
- **Clean error responses** — tools never crash; they return formatted error messages.

---

## Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `GITHUB_TOKEN` | — | GitHub PAT for API access |
| `JINA_API_KEY` | — | Jina AI reader key |
| `SE_API_KEY` | — | Stack Exchange API key |
| `MAX_FILE_LINES` | `500` | Max lines per `inspect_code` call |
| `MAX_RESPONSE_CHARS` | `8000` | Hard truncation limit per tool response |
| `REQUEST_TIMEOUT` | `30` | HTTP timeout in seconds |
| `CACHE_TTL_SECONDS` | `300` | In-memory cache expiry |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

---

## Monetization Angle

| Tier | Features | Price |
|------|----------|-------|
| **Free** | `map_repository`, `inspect_code` | $0 |
| **Pro** | + `web_to_markdown`, `search_stack_overflow` | $9/mo |
| **Team** | + `analyze_issues`, `dependency_checker`, priority support | $29/mo |
| **Enterprise** | Private deployment, custom integrations, SSO | Contact |

**Distribution vectors:**
- **Claude Desktop MCP marketplace** — list as a verified MCP server.
- **VS Code extension** — wrap the SSE server in a Copilot-compatible extension.
- **CI/CD bot** — run `dependency_checker` on every PR via GitHub Actions.
- **API-as-a-service** — expose via Stripe-metered REST endpoint.

---

## Development

```bash
# Run tests
pytest tests/ -v

# Type-check
mypy insight_link_pro/

# Lint
ruff check .
```

---

## License

MIT © 2025 Insight-Link Pro