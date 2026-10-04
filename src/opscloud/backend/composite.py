"""Composite backend routing for OpsCloud.

Routes special virtual paths (``/large_tool_results/`` and ``/conversation_history/``)
to specialized backends while delegating all standard filesystem and command
execution to the default backend (e.g. ``LocalShellBackend`` or remote sandbox).
"""

from __future__ import annotations

import atexit
from pathlib import Path
import shutil
import tempfile
from typing import Any

from deepagents.backends import (
    BackendProtocol,
    CompositeBackend as SDKCompositeBackend,
    FilesystemBackend,
)

from opscloud.config.paths import ensure_conversation_history_dir


class OpsCloudCompositeBackend(SDKCompositeBackend):
    """Composite backend routing virtual paths (/large_tool_results/, /conversation_history/)."""

    def __init__(
        self,
        default: Any,
        routes: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize OpsCloudCompositeBackend with default and routed backends.

        Args:
            default: Default backend instance to fall back to.
            routes: Optional mapping of virtual path prefixes to backend instances.
            **kwargs: Additional keyword arguments for SDKCompositeBackend.
        """
        self._large_results_dir = tempfile.mkdtemp(prefix="opscloud_large_results_")
        self._conversation_history_dir = ensure_conversation_history_dir()

        atexit.register(self.cleanup)

        large_results_backend = FilesystemBackend(
            root_dir=self._large_results_dir,
            virtual_mode=True,
        )
        conversation_history_backend = FilesystemBackend(
            root_dir=self._conversation_history_dir,
            virtual_mode=True,
        )

        effective_routes: dict[str, BackendProtocol] = {
            "/large_tool_results/": large_results_backend,
            "/conversation_history/": conversation_history_backend,
        }
        if routes:
            effective_routes.update(routes)

        super().__init__(
            default=default,
            routes=effective_routes,
            **kwargs,
        )

    def cleanup(self) -> None:
        """Clean up temporary large tool result directories on shutdown.

        Note: Conversation history is persistent and is intentionally preserved.
        """
        if hasattr(self, "_large_results_dir") and self._large_results_dir:
            shutil.rmtree(self._large_results_dir, ignore_errors=True)

    def __enter__(self) -> OpsCloudCompositeBackend:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.cleanup()


# Alias for convenience
CompositeBackend = OpsCloudCompositeBackend

__all__ = [
    "CompositeBackend",
    "OpsCloudCompositeBackend",
]
