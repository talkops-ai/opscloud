"""Convenience notification helpers for OpsCloud events.

Thin wrappers around :func:`~opscloud.integrations.hooks.dispatch_hook` and
:func:`~opscloud.integrations.hooks.dispatch_hook_fire_and_forget` that assemble
standard event payload dictionaries for callers.
"""

from __future__ import annotations

from typing import Any

from opscloud.integrations.hooks import (
    dispatch_hook,
    dispatch_hook_fire_and_forget,
)


async def notify_session_start(session_id: str, model: str) -> None:
    """Fire ``session.start`` event (awaited).

    Args:
        session_id: Unique session identifier.
        model: Active model identifier.
    """
    await dispatch_hook(
        "session.start",
        {"session_id": session_id, "model": model},
    )


async def notify_session_end(session_id: str, duration_s: float) -> None:
    """Fire ``session.end`` event (awaited).

    Args:
        session_id: Unique session identifier.
        duration_s: Total session duration in seconds.
    """
    await dispatch_hook(
        "session.end",
        {"session_id": session_id, "duration_s": duration_s},
    )


def notify_tool_use(
    tool_name: str,
    tool_id: str,
    tool_args: dict[str, Any] | None = None,
) -> None:
    """Fire ``tool.use`` event (fire-and-forget).

    Args:
        tool_name: Name of tool being invoked.
        tool_id: Unique tool execution ID.
        tool_args: Arguments passed to the tool.
    """
    dispatch_hook_fire_and_forget(
        "tool.use",
        {
            "tool_name": tool_name,
            "tool_id": tool_id,
            "tool_args": tool_args or {},
        },
    )


def notify_tool_result(
    tool_name: str,
    tool_status: str = "success",
    tool_output: str = "",
) -> None:
    """Fire ``tool.result`` event (fire-and-forget).

    Args:
        tool_name: Name of completed tool.
        tool_status: Execution status (e.g. ``"success"``, ``"error"``).
        tool_output: Tool output string.
    """
    dispatch_hook_fire_and_forget(
        "tool.result",
        {
            "tool_name": tool_name,
            "tool_status": tool_status,
            "tool_output": tool_output,
        },
    )


async def notify_task_complete(summary: str) -> None:
    """Fire ``task.complete`` event (awaited).

    Args:
        summary: Human-readable task completion summary.
    """
    await dispatch_hook("task.complete", {"summary": summary})


def notify_deploy_event(
    event_type: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Fire a DevOps deployment lifecycle event (fire-and-forget).

    Args:
        event_type: One of ``"deploy.start"``, ``"deploy.complete"``,
            ``"terraform.plan"``, ``"terraform.apply"``, ``"helm.install"``,
            ``"kubectl.apply"``.
        details: Additional payload parameters.
    """
    dispatch_hook_fire_and_forget(event_type, details or {})


def notify_cloud_event(
    event_type: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Fire a cloud infrastructure lifecycle event (fire-and-forget).

    Args:
        event_type: Cloud event name (e.g. ``"aws.resource.create"``,
            ``"aws.resource.update"``, ``"aws.resource.delete"``).
        details: Details of the cloud resource or operation.
    """
    dispatch_hook_fire_and_forget(event_type, details or {})


__all__ = [
    "notify_cloud_event",
    "notify_deploy_event",
    "notify_session_end",
    "notify_session_start",
    "notify_task_complete",
    "notify_tool_result",
    "notify_tool_use",
]
