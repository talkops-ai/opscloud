"""SSRF-protected URL fetcher converting HTML to clean markdown."""

from __future__ import annotations

from collections.abc import Callable
from html.parser import HTMLParser
from typing import Annotated, Any
from urllib.parse import urljoin, urlparse

from langchain_core.tools import tool
from markdownify import markdownify
from pydantic import Field
import requests

from opscloud.security.url_validation import (
    _pinned_dns,
    _UrlValidationError,
    _validate_url,
)
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

_MAX_FETCH_REDIRECTS = 5
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})


class _TextExtractor(HTMLParser):
    """Fallback plain-text extractor skipping script/style tags."""

    _SKIP_TAGS = frozenset({"script", "style", "noscript", "template"})

    def __init__(self) -> None:
        """Initialize _TextExtractor with empty parts and zero skip depth."""
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Track start tag and increment skip depth for ignored tags.

        Args:
            tag: HTML tag name.
            attrs: List of attribute (name, value) pairs.
        """
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        """Track end tag and decrement skip depth for ignored tags.

        Args:
            tag: HTML tag name.
        """
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        """Process text data when outside skipped tags.

        Args:
            data: Raw text data from HTML parser.
        """
        if self._skip_depth:
            return
        text = " ".join(data.split())
        if text:
            self.parts.append(text)

    def get_text(self) -> str:
        """Return accumulated extracted plain text separated by blank lines.

        Returns:
            str: Extracted plain text content.
        """
        return "\n\n".join(self.parts)


def _html_to_markdown_content(html: str, markdownify: Callable[[str], str]) -> str:
    """Safely convert HTML to markdown with a plain text fallback on recursion error."""
    try:
        return markdownify(html)
    except RecursionError:
        logger.warning("markdownify hit recursion depth; falling back to text extraction", exc_info=True)

    try:
        parser = _TextExtractor()
        parser.feed(html)
        parser.close()
        return parser.get_text()
    except Exception:
        logger.warning("text-extraction fallback failed", exc_info=True)
        return ""


def _fetch_with_redirects(url: str, *, timeout: int) -> Any:
    """Fetch URL, re-validating redirects against SSRF blocks and pinning DNS at each hop."""
    current_url = url
    session = requests.Session()
    session.trust_env = False

    for _hop in range(_MAX_FETCH_REDIRECTS + 1):
        validated_ips = _validate_url(current_url)
        hostname = urlparse(current_url).hostname
        assert hostname is not None
        encoded_hostname = hostname.encode("idna").decode("ascii")

        with _pinned_dns(encoded_hostname, validated_ips):
            response = session.get(
                current_url,
                timeout=timeout,
                headers={"User-Agent": "Mozilla/5.0 (compatible; OpsCloud/1.0)"},
                allow_redirects=False,
            )

        if 300 <= response.status_code < 400:
            location = response.headers.get("Location")
            if not location:
                msg = f"Redirect response ({response.status_code}) missing Location header"
                raise _UrlValidationError(msg)
            current_url = urljoin(current_url, location)
            continue

        response.raise_for_status()
        return response

    msg = f"Exceeded {_MAX_FETCH_REDIRECTS} redirects starting from {url!r}"
    raise requests.exceptions.TooManyRedirects(msg)


@tool
def fetch_url(
    url: Annotated[
        str,
        Field(description="The URL to fetch (must be a valid HTTP/HTTPS URL)."),
    ],
    timeout: Annotated[
        int,
        Field(description="Request timeout in seconds."),
    ] = 30,
) -> dict[str, Any]:
    """Fetch content from a URL and convert HTML to markdown format.

    This tool fetches web page content and converts it to clean markdown text,
    making it easy to read and process HTML content. After receiving the markdown,
    you MUST synthesize the information into a natural, helpful response for the user.

    Args:
        url: The URL to fetch (must be a valid HTTP/HTTPS URL)
        timeout: Request timeout in seconds (default: 30)

    Returns:
        Dictionary containing url, markdown_content, status_code, content_length, and success.
    """
    try:
        response = _fetch_with_redirects(url, timeout=timeout)
    except _UrlValidationError as e:
        return {
            "error": f"Fetch URL error: {e!s}",
            "url": url,
            "category": "validation",
        }
    except requests.exceptions.TooManyRedirects as e:
        return {"error": f"Fetch URL error: {e!s}", "url": url, "category": "redirects"}
    except requests.exceptions.RequestException as e:
        return {"error": f"Fetch URL error: {e!s}", "url": url, "category": "network"}

    markdown_content = _html_to_markdown_content(response.text, markdownify)
    return {
        "url": str(response.url),
        "markdown_content": markdown_content,
        "status_code": response.status_code,
        "content_length": len(markdown_content),
        "success": True,
    }

