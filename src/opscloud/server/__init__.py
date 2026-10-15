"""LangGraph dev server integration package."""

from typing import Any

from opscloud._constants import SERVER_ENV_PREFIX
from opscloud.server._server_config import ServerConfig
from opscloud.server.server import ServerProcess, find_free_port, generate_langgraph_json

__all__ = [
    "SERVER_ENV_PREFIX",
    "ServerConfig",
    "ServerProcess",
    "create_webhooks_app",
    "find_free_port",
    "generate_langgraph_json",
    "get_webhook_dispatcher",
    "set_webhook_dispatcher",
]


def __getattr__(name: str) -> Any:
    if name in ("create_webhooks_app", "get_webhook_dispatcher", "set_webhook_dispatcher"):
        from opscloud.server import webhooks_app

        return getattr(webhooks_app, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

