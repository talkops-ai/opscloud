"""Server-side graph entry point for ``langgraph dev``.

Module import intentionally performs no model, MCP, or graph construction. LangGraph
awaits :func:`make_graph`, and the closure-managed runtime factory builds the graph once
without blocking its graph-import worker.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import os
from pathlib import Path
from typing import Any, NamedTuple

from opscloud.backend.composite import OpsCloudCompositeBackend
from opscloud.server._server_config import ServerConfig


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


async def _build_default_runtime_async(
    *,
    checkpointer: Any | None = None,
    store: Any | None = None,
) -> ServerRuntime:
    """Prepare MCP/model resources on the event loop before graph compilation."""
    config = ServerConfig.from_env()

    if config.cwd:
        os.environ["OPSCLOUD_SERVER_CWD"] = config.cwd
    if config.project_root:
        os.environ["OPSCLOUD_SERVER_PROJECT_ROOT"] = config.project_root

    from opscloud.project_utils import get_server_project_context

    project_context = await asyncio.to_thread(get_server_project_context)
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
    from opscloud.agent.factory import create_opscloud_agent_async

    model_res = await asyncio.to_thread(create_model, config.model)
    model_res.apply_to_settings()
    effective_cwd = project_context.user_cwd if project_context else config.cwd
    agent, composite_backend = await create_opscloud_agent_async(
        model=model_res.model,
        assistant_id=config.assistant_id,
        system_prompt=config.system_prompt,
        interactive=config.interactive,
        auto_approve=config.auto_approve,
        enable_shell=config.enable_shell,
        cwd=effective_cwd,
        project_context=project_context,
        checkpointer=checkpointer,
        store=store,
    )
    return ServerRuntime(agent=agent, backend=composite_backend)


async def _make_graphs() -> ServerRuntime:
    """Create resources asynchronously, then compile the sync Deep Agents graph."""
    return await _build_default_runtime_async()


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
            runtime = await (builder or _make_graphs)()
        return runtime

    return get_runtime


_get_runtime = _build_runtime_factory()


async def make_graph() -> Any:
    """Return the agent graph for `langgraph dev`.

    Delegates to the cached runtime factory. The compiled graph is created
    on first call and served in <1ms for subsequent requests without rebuild overhead.
    """
    return (await _get_runtime()).agent
