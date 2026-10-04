"""Server-side graph entry point for `langgraph dev`."""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from opscloud.server._server_config import ServerConfig
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)


def _make_graph_sync() -> Any:
    """Synchronously create and compile the OpsCloud agent graph."""
    from opscloud.agent.factory import create_opscloud_agent
    from opscloud.model.factory import create_model
    from opscloud.project_utils import get_server_project_context

    config = ServerConfig.from_env()

    if config.cwd:
        os.environ["OPSCLOUD_SERVER_CWD"] = config.cwd
    if config.project_root:
        os.environ["OPSCLOUD_SERVER_PROJECT_ROOT"] = config.project_root

    project_context = get_server_project_context()

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

    model_spec = config.model or "anthropic:claude-3-5-sonnet-latest"
    model_res = create_model(model_spec)

    effective_cwd = project_context.user_cwd if project_context else config.cwd

    agent, _composite_backend = create_opscloud_agent(
        model=model_res.model,
        assistant_id=config.assistant_id,
        system_prompt=config.system_prompt,
        interactive=config.interactive,
        auto_approve=config.auto_approve,
        enable_shell=config.enable_shell,
        cwd=effective_cwd,
        project_context=project_context,
    )
    return agent


_precompiled_graph: Any = None
_graph_lock = asyncio.Lock()

# When running in server mode (ServerProcess sets OPSCLOUD_SERVER_ASSISTANT_ID),
# eagerly compile the graph during module import / container startup.
# Official LangChain guidance: "Move expensive initialization (API clients, DB connections,
# model loading) from graph factory if you are seeing API slowness. Export an already-compiled
# CompiledGraph instance... or keep factory functions lightweight since they run on every invocation."
if "OPSCLOUD_SERVER_ASSISTANT_ID" in os.environ:
    try:
        _precompiled_graph = _make_graph_sync()
    except Exception as exc:
        logger.critical("Failed to precompile server graph during startup: %s", exc, exc_info=True)
        print(f"Failed to precompile server graph during startup: {exc}", file=sys.stderr)
        raise


async def make_graph() -> Any:
    """Return the agent graph for `langgraph dev`.

    Returns the pre-compiled graph in <1ms without incurring per-request compilation latency.
    """
    global _precompiled_graph
    if _precompiled_graph is not None:
        return _precompiled_graph

    # Fallback for environments where OPSCLOUD_SERVER_ASSISTANT_ID wasn't pre-set
    async with _graph_lock:
        if _precompiled_graph is None:
            _precompiled_graph = await asyncio.to_thread(_make_graph_sync)
        return _precompiled_graph
