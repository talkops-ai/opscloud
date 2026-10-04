"""Event hook dispatch for opscloud.

Hooks allow external tools to react to agent events. Configured in
``~/.opscloud/hooks.json``.
"""

from __future__ import annotations

import asyncio
import json
from opscloud.utils.logger import get_logger
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = get_logger(__name__)

HOOK_SUBPROCESS_TIMEOUT = 5
"""Seconds before a hook subprocess is killed."""

HOOK_TOOL_OUTPUT_LIMIT = 2000
"""Max chars kept in tool_output payload fields."""

# Standard events
STANDARD_EVENTS = frozenset(
    {
        "session.start",
        "session.end",
        "user.prompt",
        "tool.use",
        "tool.result",
        "tool.error",
        "task.complete",
        "user.name.set",
        "context.compact",
        "permission.request",
    }
)

# Cloud / DevOps events
DEVOPS_EVENTS = frozenset(
    {
        "terraform.plan",
        "terraform.apply",
        "helm.install",
        "kubectl.apply",
        "deploy.start",
        "deploy.complete",
        "aws.resource.create",
        "aws.resource.update",
        "aws.resource.delete",
    }
)

ALL_KNOWN_EVENTS = STANDARD_EVENTS | DEVOPS_EVENTS


@dataclass(frozen=True)
class HookConfig:
    """A single hook definition from ``hooks.json``."""

    command: list[str]
    """Subprocess command to execute."""

    events: frozenset[str]
    """Events this hook listens to. Empty -> all events."""

    def matches(self, event: str) -> bool:
        """Return True if this hook should fire for event."""
        return len(self.events) == 0 or event in self.events


_hooks: list[HookConfig] = []
_pending_tasks: set[asyncio.Task[None]] = set()
_loaded = False


def _default_hooks_path() -> Path:
    from opscloud.config.paths import HOOKS_PATH
    return HOOKS_PATH


def load_hooks(path: Path | None = None) -> list[HookConfig]:
    """Parse hooks.json and populate the module-level hook list."""
    global _hooks, _loaded

    parsed: list[HookConfig] = []

    def _parse_hooks_data(data: dict[str, Any], source_name: str) -> None:
        for entry in data.get("hooks", []):
            cmd = entry.get("command")
            if not isinstance(cmd, list) or not cmd:
                logger.warning("Skipping hook with invalid command from %s: %r", source_name, entry)
                continue
            events_raw = entry.get("events", [])
            if not isinstance(events_raw, list):
                events_raw = []
            parsed.append(
                HookConfig(
                    command=cmd,
                    events=frozenset(events_raw),
                )
            )

    hooks_file = path or _default_hooks_path()
    if hooks_file.is_file():
        try:
            data = json.loads(hooks_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _parse_hooks_data(data, str(hooks_file))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to parse hooks config %s: %s", hooks_file, exc)

    # Discover and merge plugin hooks
    try:
        from opscloud.plugins.discovery import discover_plugins

        plugins = discover_plugins().plugins
        for plugin in plugins:
            plugin_hooks_file = plugin.root / "hooks" / "hooks.json"
            if plugin_hooks_file.is_file():
                try:
                    p_data = json.loads(plugin_hooks_file.read_text(encoding="utf-8"))
                    if isinstance(p_data, dict):
                        _parse_hooks_data(p_data, f"plugin:{plugin.plugin_id}")
                except (json.JSONDecodeError, OSError) as exc:
                    logger.warning("Failed to parse plugin hooks config %s: %s", plugin_hooks_file, exc)
    except Exception as exc:
        logger.debug("Plugin hook discovery skipped: %s", exc)

    _hooks = parsed
    _loaded = True
    return _hooks


def _ensure_loaded() -> list[HookConfig]:
    if not _loaded:
        load_hooks()
    return _hooks


def _sanitise_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    if "tool_output" in out and isinstance(out["tool_output"], str):
        if len(out["tool_output"]) > HOOK_TOOL_OUTPUT_LIMIT:
            out["tool_output"] = out["tool_output"][:HOOK_TOOL_OUTPUT_LIMIT] + "…[truncated]"
    return out


async def _run_hook(hook: HookConfig, event: str, payload: dict[str, Any]) -> None:
    from opscloud.hooks.env import sanitize_hook_environ

    env = {**sanitize_hook_environ(), "OPSCLOUD_HOOK_EVENT": event}
    json_payload = json.dumps(_sanitise_payload(payload))

    try:
        proc = await asyncio.create_subprocess_exec(
            *hook.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env,
        )
        _, stderr = await asyncio.wait_for(
            proc.communicate(input=json_payload.encode()),
            timeout=HOOK_SUBPROCESS_TIMEOUT,
        )
        if proc.returncode != 0:
            logger.debug(
                "Hook %s exited %d for event %s: %s",
                hook.command[0],
                proc.returncode,
                event,
                stderr.decode(errors="replace")[:200],
            )
    except asyncio.TimeoutError:
        logger.debug("Hook %s timed out after %ds for event %s", hook.command[0], HOOK_SUBPROCESS_TIMEOUT, event)
        with suppress_os_errors():
            proc.kill()  # type: ignore[possibly-undefined]
    except OSError as exc:
        logger.debug("Hook %s failed to start: %s", hook.command[0], exc)


class suppress_os_errors:
    def __enter__(self) -> None:
        pass

    def __exit__(self, exc_type: type | None, *_: object) -> bool:
        return exc_type is not None and issubclass(exc_type, OSError)


async def dispatch_hook(event: str, payload: dict[str, Any]) -> None:
    """Dispatch event to all matching hooks and await completion."""
    hooks = _ensure_loaded()
    tasks = [_run_hook(h, event, payload) for h in hooks if h.matches(event)]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def dispatch_hook_fire_and_forget(event: str, payload: dict[str, Any]) -> None:
    """Dispatch event without waiting."""
    hooks = _ensure_loaded()
    matching = [h for h in hooks if h.matches(event)]
    if not matching:
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    for hook in matching:
        task = loop.create_task(_run_hook(hook, event, payload))
        _pending_tasks.add(task)
        task.add_done_callback(_pending_tasks.discard)


async def drain_pending_hooks() -> None:
    """Wait for all fire-and-forget hooks to complete."""
    if _pending_tasks:
        await asyncio.gather(*_pending_tasks, return_exceptions=True)
        _pending_tasks.clear()


__all__ = [
    "ALL_KNOWN_EVENTS",
    "DEVOPS_EVENTS",
    "HOOK_SUBPROCESS_TIMEOUT",
    "HOOK_TOOL_OUTPUT_LIMIT",
    "HookConfig",
    "STANDARD_EVENTS",
    "dispatch_hook",
    "dispatch_hook_fire_and_forget",
    "drain_pending_hooks",
    "load_hooks",
]
