"""Trace metadata generation for LangSmith and telemetry correlation.

Provides:
- ``build_coding_agent_metadata`` — shared trace metadata block
- ``build_stream_config`` — LangGraph stream config dict

The old ``SETTINGS_METADATA`` dict, ``determine_category``, ``is_sensitive``,
``serialize_value``, and ``deserialize_value`` have been eliminated — their
functionality is now handled by the manifest (``ConfigOption``) and the
store (``ConfigStore``).
"""

from __future__ import annotations

from datetime import UTC, datetime
import os
from pathlib import Path
from typing import Any

from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

# coding-agent-v1 contract literals
CODING_AGENT_PURPOSE = "coding"
CODING_AGENT_INTEGRATION = "opscloud"
CODING_AGENT_RUNTIME = "OpsCloud"
CODING_AGENT_TRACE_SCHEMA_VERSION = "coding-agent-v1"


def _get_version() -> str:
    """Get package version."""
    try:
        from importlib.metadata import version

        return version("opscloud")
    except Exception:
        return "0.0.0"


def _get_git_branch(cwd_str: str) -> str | None:
    """Resolve the current git branch."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd_str,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
            return branch if branch else None
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _get_git_commit_sha(cwd_str: str) -> str | None:
    """Resolve the current git commit SHA."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd_str,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            sha = result.stdout.strip()
            return sha if sha else None
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def build_coding_agent_metadata(
    *,
    thread_id: str,
    turn_id: str | None = None,
    turn_number: int | None = None,
    cwd: str | Path | None = None,
    git_branch: str | None = None,
    sandbox_type: str | None = None,
    user_id: str | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Build the shared coding-agent-v1 trace-metadata block."""
    if cwd is None:
        try:
            from opscloud.project_utils import get_server_project_context

            ctx = get_server_project_context()
            cwd_str = str(ctx.user_cwd) if ctx is not None else str(Path.cwd())
        except Exception:
            cwd_str = ""
    else:
        cwd_str = str(cwd)

    version = _get_version()
    metadata: dict[str, Any] = {
        "ls_agent_purpose": CODING_AGENT_PURPOSE,
        "ls_integration": CODING_AGENT_INTEGRATION,
        "ls_agent_runtime": CODING_AGENT_RUNTIME,
        "thread_id": thread_id,
        "ls_trace_schema_version": CODING_AGENT_TRACE_SCHEMA_VERSION,
        "ls_integration_version": version,
        "ls_agent_runtime_version": version,
    }

    if turn_id:
        metadata["turn_id"] = turn_id
    if turn_number is not None:
        metadata["turn_number"] = turn_number
    if reasoning_effort:
        metadata["reasoning_effort"] = reasoning_effort

    if cwd_str:
        metadata["cwd"] = cwd_str
        effective_branch = git_branch or _get_git_branch(cwd_str)
        if effective_branch:
            metadata["git_branch"] = effective_branch
        commit_sha = _get_git_commit_sha(cwd_str)
        if commit_sha:
            metadata["git_commit_sha"] = commit_sha

    if user_id:
        metadata["user_id"] = user_id
    if sandbox_type and sandbox_type != "none":
        metadata["sandbox_type"] = sandbox_type

    return metadata


def build_stream_config(
    thread_id: str,
    assistant_id: str | None = None,
    *,
    sandbox_type: str | None = None,
    turn_id: str | None = None,
    turn_number: int | None = None,
    approval_mode: str | None = None,
    approval_mode_key: str | None = None,
    auto_approve: bool = False,
    cwd: str | Path | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Build the LangGraph stream config dict including trace metadata."""
    if cwd is None:
        try:
            from opscloud.project_utils import get_server_project_context

            ctx = get_server_project_context()
            effective_cwd = str(ctx.user_cwd) if ctx is not None else str(Path.cwd())
        except Exception:
            effective_cwd = ""
    else:
        effective_cwd = str(cwd)

    effective_user_id = os.environ.get("OPSCLOUD_USER_ID") or None
    effective_branch = _get_git_branch(effective_cwd) if effective_cwd else None

    metadata: dict[str, Any] = build_coding_agent_metadata(
        thread_id=thread_id,
        turn_id=turn_id,
        turn_number=turn_number,
        cwd=effective_cwd,
        git_branch=effective_branch,
        sandbox_type=sandbox_type,
        user_id=effective_user_id,
        reasoning_effort=reasoning_effort,
    )

    if auto_approve:
        metadata["auto_approve"] = True
    if approval_mode:
        metadata["approval_mode"] = approval_mode
    if approval_mode_key:
        metadata["approval_mode_key"] = approval_mode_key

    version = _get_version()
    metadata["lc_versions"] = {"opscloud": version}

    if assistant_id:
        metadata.update(
            {
                "agent_name": assistant_id,
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )

    configurable: dict[str, Any] = {"thread_id": thread_id}
    if approval_mode:
        configurable["approval_mode"] = approval_mode
    if approval_mode_key:
        configurable["approval_mode_key"] = approval_mode_key

    return {
        "configurable": configurable,
        "metadata": metadata,
    }
