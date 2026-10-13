"""Server lifecycle manager for OpsCloud CLI."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator

from opscloud.server import ServerConfig, ServerProcess
from opscloud.ui.remote_client import RemoteAgent
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)


@contextmanager
def server_session(config: ServerConfig) -> Generator[RemoteAgent, None, None]:
    """Launch server subprocess and yield a connected RemoteAgent client."""
    proc = ServerProcess(config)
    try:
        url = proc.start()
        agent = RemoteAgent(url)
        yield agent
    finally:
        proc.stop()


async def start_server_and_get_agent(
    *,
    assistant_id: str = "opscloud",
    model_name: str | None = None,
    interactive: bool = True,
    auto_approve: bool = False,
    cwd: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 0,
    on_spawn: Callable[[ServerProcess], None] | None = None,
    **kwargs: Any,
) -> tuple[RemoteAgent, ServerProcess]:
    """Start a LangGraph dev server asynchronously and return a RemoteAgent client."""
    from opscloud.project_utils import ProjectContext

    project_context = (
        ProjectContext.from_user_cwd(Path(cwd))
        if cwd is not None
        else ProjectContext.from_user_cwd(Path.cwd())
    )
    resolved_model = model_name or kwargs.pop("model", None)
    if not resolved_model:
        try:
            from opscloud.model.factory import _get_default_model_spec

            resolved_model = _get_default_model_spec()
        except Exception:
            resolved_model = None

    config = ServerConfig.from_cli_args(
        project_context=project_context,
        assistant_id=assistant_id,
        model=resolved_model,
        interactive=interactive,
        approval_mode="never" if auto_approve else "auto",
        **kwargs,
    )
    server = ServerProcess(config, host=host, port=port)
    if on_spawn is not None:
        try:
            on_spawn(server)
        except Exception:
            pass

    # Schedule background database maintenance on launch (DV-08)
    try:
        from opscloud.state.session import SessionManager
        import asyncio
        loop = asyncio.get_running_loop()
        loop.create_task(SessionManager.maintain_database())
    except Exception:
        pass

    url = await server.astart()
    agent = RemoteAgent(url=url, graph_name="agent")
    return agent, server
