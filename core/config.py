"""
Configuration management for Insight-Link Pro.
Loads environment variables and exposes typed settings.
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class AppConfig:
    """Central configuration object populated from environment variables."""

    # GitHub
    github_token: str = field(default_factory=lambda: os.getenv("GITHUB_TOKEN", ""))
    github_api_base: str = "https://api.github.com"

    # Jina AI (web-to-markdown)
    jina_api_key: str = field(default_factory=lambda: os.getenv("JINA_API_KEY", ""))
    jina_base_url: str = "https://r.jina.ai"

    # Stack Overflow / SE API
    se_api_key: str = field(default_factory=lambda: os.getenv("SE_API_KEY", ""))
    se_api_base: str = "https://api.stackexchange.com/2.3"

    # Safety / limits
    max_file_lines: int = int(os.getenv("MAX_FILE_LINES", "500"))
    max_response_chars: int = int(os.getenv("MAX_RESPONSE_CHARS", "8000"))
    request_timeout: int = int(os.getenv("REQUEST_TIMEOUT", "30"))
    cache_ttl_seconds: int = int(os.getenv("CACHE_TTL_SECONDS", "300"))

    # Server
    server_name: str = os.getenv("SERVER_NAME", "insight-link-pro")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    # OSV vulnerability API (free, no key needed)
    osv_api_base: str = "https://api.osv.dev/v1"

    def validate(self) -> list[str]:
        """Return a list of warnings for missing optional credentials."""
        warnings: list[str] = []
        if not self.github_token:
            warnings.append("GITHUB_TOKEN not set – GitHub rate limits apply (60 req/hr).")
        if not self.jina_api_key:
            warnings.append("JINA_API_KEY not set – web_to_markdown will use unauthenticated endpoint.")
        if not self.se_api_key:
            warnings.append("SE_API_KEY not set – Stack Overflow quota limited to 300 req/day.")
        return warnings


# Singleton
config = AppConfig()