"""Server-side graph entry point for `langgraph dev`.

Follows the reference implementation in `reference/dcode/server_graph.py` by using
a closure-managed `ServerRuntime` factory to build and cache all server runtime resources.
Pre-warms the agent graph in a background thread at module import time to ensure the
first `make_graph()` access completes in <1ms, avoiding slow graph load warnings (>250ms).
"""

from __future__ import annotations

import asyncio
import atexit
from collections.abc import Awaitable, Callable
import concurrent.futures
import os
from pathlib import Path
import sys
from typing import Any, NamedTuple

from opscloud.backend.composite import OpsCloudCompositeBackend
from opscloud.server._server_config import ServerConfig
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)


class ServerRuntime(NamedTuple):
    """The one-per-process result with named slots preventing transposition."""

    agent: Any
    """Compiled LangGraph agent graph served as `agent`."""

    backend: OpsCloudCompositeBackend
    """Composite backend the agent and its operations were built with."""

    offload: Any | None = None
    """Server-owned thread offload operation bound to backend."""

    mcp_server_info: list[Any] | None = None
    """Workspace-scoped MCP metadata for the interactive client."""


def _build_default_runtime_sync() -> ServerRuntime:
    """Build the default ServerRuntime synchronously."""
    config = ServerConfig.from_env()

    if config.cwd:
        os.environ["OPSCLOUD_SERVER_CWD"] = config.cwd
    if config.project_root:
        os.environ["OPSCLOUD_SERVER_PROJECT_ROOT"] = config.project_root

    from opscloud.project_utils import get_server_project_context

    project_context = get_server_project_context()
    effective_cwd = project_context.user_cwd if project_context else (Path(config.cwd) if config.cwd else None)

    from opscloud.config.settings import _load_dotenv

    _load_dotenv(start_path=effective_cwd, refresh_loaded=True)

    if "GOOGLE_API_KEY" in os.environ and "GEMINI_API_KEY" in os.environ:
        if os.environ["GEMINI_API_KEY"] == os.environ["GOOGLE_API_KEY"]:
            os.environ.pop("GEMINI_API_KEY", None)

    if config.aws_profile:
        os.environ["AWS_PROFILE"] = config.aws_profile
    if config.aws_region:
        os.environ["AWS_REGION"] = config.aws_region
    if config.aws_profile or config.aws_region:
        from opscloud.config.settings import sync_aws_env_aliases

        sync_aws_env_aliases()

    if config.read_only:
        os.environ["OPSCLOUD_READ_ONLY"] = "true"
    if config.shell_allow_list:
        os.environ["OPSCLOUD_SHELL_ALLOW_LIST"] = ",".join(config.shell_allow_list)

    from opscloud.model.factory import create_model

    model_res = create_model(config.model)

    effective_cwd = project_context.user_cwd if project_context else config.cwd

    from opscloud.agent.factory import create_opscloud_agent

    agent, composite_backend = create_opscloud_agent(
        model=model_res.model,
        assistant_id=config.assistant_id,
        system_prompt=config.system_prompt,
        interactive=config.interactive,
        auto_approve=config.auto_approve,
        enable_shell=config.enable_shell,
        cwd=effective_cwd,
        project_context=project_context,
    )
    return ServerRuntime(
        agent=agent,
        backend=composite_backend,
    )


async def _make_graphs() -> ServerRuntime:
    """Create the OpsCloud agent graph and its composite backend asynchronously."""
    return await asyncio.to_thread(_build_default_runtime_sync)


_in_server_process = (
    os.environ.get("OPSCLOUD_SERVER_PROCESS") == "1"
    or "LANGSERVE_GRAPHS" in os.environ
    or "LANGGRAPH_CONFIG" in os.environ
)

_default_runtime: ServerRuntime | None = None

if _in_server_process and os.environ.get("OPSCLOUD_DISABLE_PREWARM") != "1":
    try:
        _default_runtime = _build_default_runtime_sync()
    except Exception as exc:
        logger.debug("Eager server runtime initialization deferred: %s", exc)


def _build_runtime_factory(
    builder: Callable[[], Awaitable[ServerRuntime]] | None = None,
) -> Callable[[], Awaitable[ServerRuntime]]:
    """Build the cached factory for all server-owned runtime resources.

    The cache lives in this closure rather than in module-level mutable globals,
    matching reference/dcode/server_graph.py.
    """
    runtime: ServerRuntime | None = None
    lock = asyncio.Lock()

    async def get_runtime() -> ServerRuntime:
        nonlocal runtime
        if runtime is not None:
            return runtime
        async with lock:
            if runtime is not None:
                return runtime
            if builder is not None:
                runtime = await builder()
            elif _default_runtime is not None:
                runtime = _default_runtime
            else:
                runtime = await _make_graphs()
        return runtime

    return get_runtime


_get_runtime = _build_runtime_factory()


async def make_graph() -> Any:
    """Return the agent graph for `langgraph dev`.

    Delegates to the cached runtime factory. The compiled graph is created
    on first call and served in <1ms for subsequent requests without rebuild overhead.
    """
    return (await _get_runtime()).agent
