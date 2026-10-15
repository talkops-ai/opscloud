"""Server lifecycle manager for OpsCloud CLI."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AsyncExitStack, contextmanager
import os
from pathlib import Path
from typing import Any, Generator

from opscloud.server import ServerConfig, ServerProcess
from opscloud.ui.remote_client import InProcessAgent, RemoteAgent
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)


def _use_in_process_server(port: int = 0, explicit: bool | None = None) -> bool:
    """Return True if OpsCloud should run the LangGraph backend in-process."""
    if explicit is not None:
        return bool(explicit)
    if port != 0:
        return False
    raw = os.environ.get("OPSCLOUD_IN_PROCESS_SERVER", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


class InProcessServerHandle:
    """In-process backend lifecycle handle matching ``ServerProcess``'s interface.

    Compiles and runs the LangGraph ``Pregel`` graph, SQLite checkpointer, and
    ``InMemoryStore`` directly inside the CLI process without spawning a separate
    ``langgraph dev`` HTTP server subprocess.
    """

    is_in_process: bool = True

    def __init__(
        self,
        config: ServerConfig,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self.config = config
        self.host = host
        self.port = port
        self.url = "inprocess://local"
        self.process: Any = None
        self.graph: Any = None
        self.backend: Any = None
        self.checkpointer: Any = None
        self.store: Any = None
        self._exit_stack: AsyncExitStack | None = None

    def _apply_config_to_env(self) -> None:
        for key, value in self.config.to_env().items():
            if value is not None:
                os.environ[key] = str(value)

    async def astart(self, timeout: float = 60.0) -> str:
        """Initialize the SQLite checkpointer, store, and compiled graph in-process."""
        del timeout
        self._apply_config_to_env()

        if self._exit_stack is not None:
            try:
                await self._exit_stack.aclose()
            except Exception:
                pass

        stack = AsyncExitStack()
        self._exit_stack = stack

        try:
            from langgraph.store.memory import InMemoryStore
            from opscloud.state.session import OpsCloudCheckpointer, get_db_path

            db_path = str(get_db_path())
            os.environ["OPSCLOUD_SERVER_DB_PATH"] = db_path
            self.checkpointer = await stack.enter_async_context(
                OpsCloudCheckpointer.from_conn_string(db_path)
            )
            self.store = InMemoryStore()
        except Exception as exc:
            logger.warning("In-process checkpointer init fallback: %s", exc)
            from langgraph.checkpoint.memory import InMemorySaver
            from langgraph.store.memory import InMemoryStore

            self.checkpointer = InMemorySaver()
            self.store = InMemoryStore()

        from opscloud.server.server_graph import _build_default_runtime_async

        runtime = await _build_default_runtime_async(
            checkpointer=self.checkpointer,
            store=self.store,
        )
        self.graph = runtime.agent
        self.backend = runtime.backend
        return self.url

    def start(self, timeout: float = 60.0) -> str:
        """Synchronous compatibility wrapper around ``astart``."""
        return asyncio.run(self.astart(timeout=timeout))

    async def astop(self) -> None:
        """Close the in-process checkpointer context cleanly."""
        stack = self._exit_stack
        self._exit_stack = None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception as exc:
                logger.debug("Error closing InProcessServerHandle stack: %s", exc)

    def stop(self) -> None:
        """Stop the in-process handle and schedule async checkpointer teardown if needed."""
        stack = self._exit_stack
        self._exit_stack = None
        if stack is None:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(stack.aclose())
        except RuntimeError:
            try:
                asyncio.run(stack.aclose())
            except Exception:
                pass

    async def restart(self, timeout: float = 60.0) -> str:
        """Rebuild the in-process agent graph (used by `/reload` and `/restart`)."""
        await self.astop()
        return await self.astart(timeout=timeout)

    def create_client(self) -> InProcessAgent:
        """Return an ``InProcessAgent`` bound to the compiled graph."""
        return InProcessAgent(
            self.graph,
            checkpointer=self.checkpointer,
            store=self.store,
            graph_name="agent",
            cwd=self.config.cwd,
        )


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
    on_spawn: Callable[[Any], None] | None = None,
    in_process: bool | None = None,
    **kwargs: Any,
) -> tuple[RemoteAgent, Any]:
    """Start the OpsCloud backend (in-process by default, or subprocess) and return a client."""
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

    # Schedule background database maintenance on launch (DV-08)
    try:
        from opscloud.state.session import SessionManager

        loop = asyncio.get_running_loop()
        loop.create_task(SessionManager.maintain_database())
    except Exception:
        pass

    if _use_in_process_server(port=port, explicit=in_process):
        handle = InProcessServerHandle(config, host=host, port=port)
        if on_spawn is not None:
            try:
                on_spawn(handle)
            except Exception:
                pass
        await handle.astart()
        return handle.create_client(), handle

    server = ServerProcess(config, host=host, port=port)
    if on_spawn is not None:
        try:
            on_spawn(server)
        except Exception:
            pass

    url = await server.astart()
    agent = RemoteAgent(url=url, graph_name="agent")
    return agent, server

