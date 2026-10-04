"""Web search tool utilizing the Tavily API in OpsCloud."""

from __future__ import annotations

import functools
import os
from typing import TYPE_CHECKING, Annotated, Any, Literal

from langchain_core.tools import BaseTool, tool
from pydantic import Field
import requests

from opscloud.config.settings import get_settings
from opscloud.utils.logger import get_logger

if TYPE_CHECKING:
    from tavily import TavilyClient

logger = get_logger(__name__)

_UNSET = object()
_tavily_client: TavilyClient | object | None = _UNSET

_WEB_SEARCH_MARKER = "opscloud_web_search"
"""Tool-metadata key marking a workspace-bound or default `web_search` variant."""

_WEB_SEARCH_TOKEN = object()
"""Value `is_web_search_tool` requires under `_WEB_SEARCH_MARKER`."""


def _missing_tavily_key_error(query: object) -> dict[str, Any]:
    """Return the payload the model sees when no Tavily key is configured."""
    return {
        "error": (
            "Tavily API key is not configured. Web search cannot be performed. "
            "Please set the TAVILY_API_KEY environment variable or configure "
            "credentials.tavily in ~/.opscloud/config.toml to enable web search."
        ),
        "query": query,
    }


def _missing_package_error(exc: ImportError) -> dict[str, Any]:
    """Return the payload the model sees when an optional package is absent."""
    return {"error": f"Required package not installed: {exc.name}. Please install tavily-python."}


def _get_tavily_client() -> TavilyClient | None:
    """Get or initialize the lazy Tavily client singleton."""
    global _tavily_client
    if _tavily_client is not _UNSET:
        return _tavily_client  # type: ignore[return-value]

    settings = get_settings()
    api_key = getattr(settings, "tavily_api_key", None) or os.environ.get("TAVILY_API_KEY")
    if api_key:
        try:
            from tavily import TavilyClient as _TavilyClient

            _tavily_client = _TavilyClient(api_key=api_key)
        except ImportError:
            logger.warning("tavily-python package is not installed.")
            _tavily_client = None
    else:
        _tavily_client = None

    return _tavily_client  # type: ignore[return-value]


def _search_with_tavily(
    client: TavilyClient,
    *,
    query: str,
    max_results: int,
    topic: Literal["general", "news", "finance"],
    include_raw_content: bool,
) -> dict[str, Any]:
    """Execute a Tavily search with standard error translation."""
    try:
        from tavily import (
            BadRequestError,
            InvalidAPIKeyError,
            MissingAPIKeyError,
            UsageLimitExceededError,
        )
        from tavily.errors import ForbiddenError, TimeoutError as TavilyTimeoutError
    except ImportError as exc:
        return _missing_package_error(exc)

    try:
        res = client.search(
            query,
            max_results=max_results,
            include_raw_content=include_raw_content,
            topic=topic,
        )
        if isinstance(res, dict):
            return res
        return {"results": res, "query": query}
    except (
        requests.exceptions.RequestException,
        ValueError,
        TypeError,
        BadRequestError,
        ForbiddenError,
        InvalidAPIKeyError,
        MissingAPIKeyError,
        TavilyTimeoutError,
        UsageLimitExceededError,
    ) as e:
        return {"error": f"Web search error: {e!s}", "query": query}


def _web_search(
    query: Annotated[
        str,
        Field(description="The search query (be specific and detailed)."),
    ],
    max_results: Annotated[
        int,
        Field(description="Number of results to return."),
    ] = 5,
    topic: Annotated[
        Literal["general", "news", "finance"],
        Field(
            description=(
                'Search topic type: "general" for most queries, "news" for current events, or "finance".'
            )
        ),
    ] = "general",
    include_raw_content: Annotated[
        bool,
        Field(
            description=(
                "Include full page content (uses more tokens). Prefer `fetch_url` for a single URL."
            )
        ),
    ] = False,
) -> dict[str, Any]:
    """Search the web for current information, cloud documentation, and troubleshooting guides.

    This tool searches the web and returns relevant results. After receiving results,
    you MUST synthesize the information into a natural, helpful response for the user.

    Returns:
        Search hits with title, URL, snippet, and score, or an error payload.
    """
    client = _get_tavily_client()
    if client is None:
        return _missing_tavily_key_error(query)
    return _search_with_tavily(
        client,
        query=query,
        max_results=max_results,
        topic=topic,
        include_raw_content=include_raw_content,
    )


web_search: BaseTool = tool("web_search")(_web_search)
web_search.metadata = {
    **(web_search.metadata or {}),
    _WEB_SEARCH_MARKER: _WEB_SEARCH_TOKEN,
}


def create_web_search_tool(api_key: str) -> BaseTool:
    """Bind web search to one workspace credential or explicit API key.

    Args:
        api_key: Tavily API key for workspace authentication.

    Returns:
        Workspace-bound web search tool.
    """
    client: TavilyClient | None = None

    @tool("web_search")
    @functools.wraps(_web_search)
    def workspace_web_search(**kwargs: Any) -> dict[str, Any]:
        nonlocal client
        if not api_key:
            return _missing_tavily_key_error(kwargs.get("query"))
        if client is None:
            try:
                from tavily import TavilyClient as _TavilyClient

                client = _TavilyClient(api_key=api_key)
            except ImportError as exc:
                return _missing_package_error(exc)
        return _search_with_tavily(client, **kwargs)


    workspace_web_search.metadata = {
        **(workspace_web_search.metadata or {}),
        _WEB_SEARCH_MARKER: _WEB_SEARCH_TOKEN,
    }
    return workspace_web_search



def is_web_search_tool(candidate: object) -> bool:
    """Return whether candidate is a built-in or workspace-bound search tool."""
    if candidate is web_search:
        return True
    metadata = getattr(candidate, "metadata", None) or {}
    return metadata.get(_WEB_SEARCH_MARKER) is _WEB_SEARCH_TOKEN


__all__ = [
    "create_web_search_tool",
    "is_web_search_tool",
    "web_search",
]

