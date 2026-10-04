"""LangSmith tracing helpers, project URL resolution, and environment synchronization."""

from __future__ import annotations

import inspect
import os
import threading
from typing import Any

from opscloud.config.settings import resolve_env_var
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

_LANGSMITH_URL_LOOKUP_TIMEOUT_SECONDS = 3.0
_langsmith_url_cache: tuple[str, str] | None = None


class LangSmithLookupError(Exception):
    """Base class for typed LangSmith project URL lookup failures."""


class LangSmithImportError(LangSmithLookupError):
    """The `langsmith` package is not installed."""


class LangSmithLookupTimeoutError(LangSmithLookupError):
    """The LangSmith project URL lookup exceeded its hard timeout."""


class LangSmithApiError(LangSmithLookupError):
    """The LangSmith SDK call raised — auth, 404, network, etc."""


class LangSmithProjectNotFoundError(LangSmithApiError):
    """The LangSmith project does not exist yet (lookup returned 404)."""


def _is_langsmith_not_found(exc: Exception) -> bool:
    """Whether a LangSmith SDK error indicates the project does not exist."""
    try:
        from langsmith.utils import LangSmithNotFoundError

        return isinstance(exc, LangSmithNotFoundError)
    except ImportError:
        return False


def _assemble_langsmith_thread_url(project_url: str, thread_id: str) -> str:
    """Format a LangSmith thread URL from a project URL prefix."""
    return f"{project_url.rstrip('/')}/t/{thread_id}?utm_source=opscloud"


def get_langsmith_project_name() -> str | None:
    """Resolve the LangSmith project name if tracing is configured."""
    langsmith_key = resolve_env_var("LANGSMITH_API_KEY") or resolve_env_var("LANGCHAIN_API_KEY")
    langsmith_tracing = resolve_env_var("LANGSMITH_TRACING") or resolve_env_var("LANGCHAIN_TRACING_V2")

    tracing_active = str(langsmith_tracing).strip().lower() in ("true", "1", "yes", "on")
    if not (langsmith_key and tracing_active):
        return None

    project = (
        resolve_env_var("OPSCLOUD_LANGSMITH_PROJECT")
        or resolve_env_var("LANGSMITH_PROJECT")
        or resolve_env_var("LANGCHAIN_PROJECT")
        or "opscloud"
    )
    return project.strip() or "opscloud"


def apply_tracing_settings(settings: Any = None) -> bool:
    """Synchronize LangSmith / LangChain tracing environment variables and reset SDK caches."""
    if settings is None:
        from opscloud.config.settings import get_settings

        settings = get_settings()

    api_key = (
        getattr(settings, "langchain_api_key", None)
        or resolve_env_var("LANGSMITH_API_KEY")
        or resolve_env_var("LANGCHAIN_API_KEY")
    )

    tracing_val = (
        getattr(settings, "langchain_tracing", None)
        if getattr(settings, "langchain_tracing", None) is not None
        else (resolve_env_var("LANGSMITH_TRACING") or resolve_env_var("LANGCHAIN_TRACING_V2"))
    )

    project = (
        getattr(settings, "langchain_project", None)
        or resolve_env_var("LANGSMITH_PROJECT")
        or resolve_env_var("LANGCHAIN_PROJECT")
        or "opscloud"
    )

    endpoint = (
        getattr(settings, "langchain_endpoint", None)
        or resolve_env_var("LANGSMITH_ENDPOINT")
        or resolve_env_var("LANGCHAIN_ENDPOINT")
    )

    is_enabled = False
    if tracing_val is not None:
        if isinstance(tracing_val, bool):
            is_enabled = tracing_val
        else:
            is_enabled = str(tracing_val).strip().lower() in ("true", "1", "yes", "on")

    # Bridge environment variables
    if is_enabled and api_key:
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        os.environ["LANGSMITH_TRACING"] = "true"
        os.environ["LANGCHAIN_API_KEY"] = str(api_key).strip()
        os.environ["LANGSMITH_API_KEY"] = str(api_key).strip()
        os.environ["LANGCHAIN_PROJECT"] = str(project).strip()
        os.environ["LANGSMITH_PROJECT"] = str(project).strip()
        if endpoint:
            os.environ["LANGCHAIN_ENDPOINT"] = str(endpoint).strip()
            os.environ["LANGSMITH_ENDPOINT"] = str(endpoint).strip()
    else:
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
        os.environ["LANGSMITH_TRACING"] = "false"

    # Invalidate LangSmith LRU cache so SDK picks up changed env vars dynamically
    try:
        import langsmith.utils

        cache_clear = getattr(langsmith.utils.get_env_var, "cache_clear", None)
        if callable(cache_clear):
            cache_clear()
    except Exception:
        pass

    enable_full_middleware_tracing()

    return is_enabled and bool(api_key)


def enable_full_middleware_tracing() -> None:
    """Ensure all middleware hook spans record full state payloads in LangSmith.

    In deepagents>=0.7.2, several SDK middlewares default to TracePolicy(process_inputs=omit_payload)
    which redacts/omits the state input dictionary ({}) in trace spans.
    Setting trace_policy = None restores complete input recording across all middlewares.
    """
    # Probe and configure trace_policy across all available middleware modules
    # using feature-detection try/except imports.
    try:
        import deepagents.middleware as dm  # Lazy import: feature detection probe

        for _, obj in inspect.getmembers(dm):
            if inspect.isclass(obj) and hasattr(obj, "trace_policy"):
                setattr(obj, "trace_policy", None)
    except Exception:
        pass

    try:
        from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware

        PatchToolCallsMiddleware.trace_policy = None
    except Exception:
        pass

    try:
        from deepagents.middleware.filesystem import FilesystemMiddleware

        FilesystemMiddleware.trace_policy = None
    except Exception:
        pass

    try:
        from deepagents.middleware.subagents import SubAgentMiddleware

        SubAgentMiddleware.trace_policy = None
    except Exception:
        pass

    try:
        from deepagents.middleware.summarization import (
            SummarizationToolMiddleware,
            _DeepAgentsSummarizationMiddleware,
        )

        SummarizationToolMiddleware.trace_policy = None
        _DeepAgentsSummarizationMiddleware.trace_policy = None
    except Exception:
        pass

    try:
        from deepagents.middleware.memory import MemoryMiddleware

        MemoryMiddleware.trace_policy = None
    except Exception:
        pass

    try:
        from deepagents.middleware.skills import SkillsMiddleware

        SkillsMiddleware.trace_policy = None
    except Exception:
        pass

    try:
        from deepagents.middleware.rubric import RubricMiddleware

        RubricMiddleware.trace_policy = None
    except Exception:
        pass

    try:
        from opscloud.middleware.skills import PluginSkillsMiddleware

        PluginSkillsMiddleware.trace_policy = None
    except Exception:
        pass

    try:
        from opscloud.middleware.reliable_rubric import ReliableRubricMiddleware

        ReliableRubricMiddleware.trace_policy = None
    except Exception:
        pass

    try:
        from opscloud.middleware.compaction import CLICompactionMiddleware

        CLICompactionMiddleware.trace_policy = None
    except Exception:
        pass


# Ensure tracing is initialized at import time as well
enable_full_middleware_tracing()


def fetch_langsmith_project_url_or_raise(project_name: str) -> str:
    """Fetch the LangSmith project URL, raising on any failure."""
    global _langsmith_url_cache

    if _langsmith_url_cache is not None:
        cached_name, cached_url = _langsmith_url_cache
        if cached_name == project_name:
            return cached_url

    try:
        from langsmith import Client
    except ImportError as exc:
        logger.debug("langsmith package not installed; cannot fetch project URL for '%s'", project_name)
        raise LangSmithImportError("langsmith package is not installed") from exc

    result: str | None = None
    lookup_error: Exception | None = None
    done = threading.Event()

    def _lookup_url() -> None:
        nonlocal result, lookup_error
        try:
            api_key = resolve_env_var("LANGSMITH_API_KEY") or resolve_env_var("LANGCHAIN_API_KEY")
            endpoint = resolve_env_var("LANGSMITH_ENDPOINT") or resolve_env_var("LANGCHAIN_ENDPOINT")
            client = Client(api_key=api_key, api_url=endpoint) if endpoint else Client(api_key=api_key)
            project = client.read_project(project_name=project_name)
            result = project.url or None
        except Exception as exc:
            lookup_error = exc
        finally:
            done.set()

    thread = threading.Thread(target=_lookup_url, daemon=True)
    thread.start()

    if not done.wait(_LANGSMITH_URL_LOOKUP_TIMEOUT_SECONDS):
        logger.debug(
            "Timed out fetching LangSmith project URL for '%s' after %.1fs",
            project_name,
            _LANGSMITH_URL_LOOKUP_TIMEOUT_SECONDS,
        )
        raise LangSmithLookupTimeoutError(
            f"LangSmith project URL lookup timed out after {_LANGSMITH_URL_LOOKUP_TIMEOUT_SECONDS:.1f}s"
        )

    if lookup_error is not None:
        logger.debug("Could not fetch LangSmith project URL for '%s'", project_name, exc_info=lookup_error)
        msg = str(lookup_error) or repr(lookup_error)
        if _is_langsmith_not_found(lookup_error):
            raise LangSmithProjectNotFoundError(msg) from lookup_error
        raise LangSmithApiError(msg) from lookup_error

    if not result:
        raise LangSmithApiError(f"LangSmith returned no URL for project '{project_name}'")

    _langsmith_url_cache = (project_name, result)
    return result


def get_cached_langsmith_thread_url(thread_id: str) -> str | None:
    """Build a LangSmith thread URL only when its project URL is cached."""
    project_name = get_langsmith_project_name()
    if not project_name or _langsmith_url_cache is None:
        return None

    cached_name, cached_url = _langsmith_url_cache
    if cached_name != project_name:
        return None
    return _assemble_langsmith_thread_url(cached_url, thread_id)


def build_langsmith_thread_url(thread_id: str) -> str | None:
    """Build a full LangSmith thread URL if tracing is configured."""
    project_name = get_langsmith_project_name()
    if not project_name:
        return None

    try:
        project_url = fetch_langsmith_project_url_or_raise(project_name)
        return _assemble_langsmith_thread_url(project_url, thread_id)
    except Exception:
        return None
