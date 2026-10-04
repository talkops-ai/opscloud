"""Bidirectional mapping between platform thread IDs and LangGraph thread IDs.

LangGraph requires a ``thread_id`` in the ``configurable`` dict for
checkpointing and interrupt / resume.  Each messaging platform has its
own thread identifier format (Slack ``thread_ts``, Teams
``conversationId``, etc.).

This store creates stable UUID-based LangGraph thread IDs and
maintains the mapping in both directions so that:

1. An incoming platform event can be mapped to its LangGraph thread.
2. An outgoing notification (e.g. from a cron-triggered run) can be
   mapped back to the correct platform thread for delivery.
"""

from __future__ import annotations

import uuid


class ThreadStore:
    """Maps ``platform:thread_id`` ↔ ``langgraph_thread_id``.

    The in-memory backend is suitable for development and single-process
    deployments.  For production multi-replica setups, swap to a
    persistent backend (Postgres, Redis) by subclassing or passing
    a ``backend`` parameter.
    """

    def __init__(self, backend: str = "memory") -> None:
        """Initialize ThreadStore mapping platform and LangGraph threads.

        Args:
            backend: Storage backend identifier (default: "memory").
        """
        # In-memory dicts keyed by composite key "platform:thread_id"
        self._platform_to_lg: dict[str, str] = {}
        self._lg_to_platform: dict[str, dict[str, str]] = {}

    # ── Forward lookup ────────────────────────────────────────────────

    def resolve(self, platform: str, platform_thread_id: str) -> str:
        """Get or create a LangGraph thread_id for a platform thread.

        Thread IDs are stable — the same platform thread always maps to
        the same LangGraph thread.

        Args:
            platform: Platform key (``"slack"``, ``"teams"``, etc.).
            platform_thread_id: Platform-native thread identifier.

        Returns:
            A UUID string used as LangGraph's ``thread_id``.
        """
        key = f"{platform}:{platform_thread_id}"
        if key not in self._platform_to_lg:
            lg_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, key))
            self._platform_to_lg[key] = lg_id
            self._lg_to_platform[lg_id] = {
                "platform": platform,
                "thread_id": platform_thread_id,
            }
        return self._platform_to_lg[key]

    # ── Reverse lookup ────────────────────────────────────────────────

    def reverse_lookup(self, lg_thread_id: str) -> dict[str, str] | None:
        """Look up platform info from a LangGraph thread_id.

        Returns:
            A dict ``{"platform": "slack", "thread_id": "..."}``
            or ``None`` if the LangGraph thread_id is unknown.
        """
        return self._lg_to_platform.get(lg_thread_id)

    # ── Existence check ───────────────────────────────────────────────

    def has_thread(self, platform: str, platform_thread_id: str) -> bool:
        """Check if a platform thread already has a LangGraph mapping."""
        key = f"{platform}:{platform_thread_id}"
        return key in self._platform_to_lg
