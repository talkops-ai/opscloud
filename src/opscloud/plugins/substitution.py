"""Variable substitution for plugin and subagent configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from opscloud.plugins.models import JsonValue


def plugin_environment(
    *,
    plugin_root: Path,
    plugin_data: Path | None = None,
    project_dir: Path | None = None,
) -> dict[str, str]:
    """Build environment variables exposed to plugin subprocesses.

    Args:
        plugin_root: Plugin root directory.
        plugin_data: Plugin data directory.
        project_dir: Optional project directory.

    Returns:
        Environment variables for plugin subprocesses.
    """
    root = str(plugin_root)
    effective_data = plugin_data if plugin_data is not None else (plugin_root / ".data")
    data = str(effective_data)
    env = {
        "CLAUDE_PLUGIN_ROOT": root,
        "CLAUDE_PLUGIN_DATA": data,
        "CODEX_PLUGIN_ROOT": root,
        "CODEX_PLUGIN_DATA": data,
        "PLUGIN_ROOT": root,
        "PLUGIN_DATA": data,
    }
    if project_dir is not None:
        proj = str(project_dir)
        env["CLAUDE_PROJECT_DIR"] = proj
        env["CODEX_PROJECT_DIR"] = proj
        env["PROJECT_DIR"] = proj
    return env


def substitute_string(
    value: str,
    *,
    plugin_root: Path,
    plugin_data: Path | None = None,
    project_dir: Path | None = None,
) -> str:
    """Substitute plugin path variables in a string.

    Args:
        value: String to transform.
        plugin_root: Plugin root directory.
        plugin_data: Plugin data directory.
        project_dir: Optional project directory.

    Returns:
        String with supported plugin variables substituted.
    """
    env = plugin_environment(
        plugin_root=plugin_root,
        plugin_data=plugin_data,
        project_dir=project_dir,
    )
    result = value
    for key, replacement in env.items():
        result = result.replace(f"${{{key}}}", replacement)
    return result


def substitute_json(
    value: JsonValue,
    *,
    plugin_root: Path,
    plugin_data: Path | None = None,
    project_dir: Path | None = None,
) -> JsonValue:
    """Substitute plugin variables throughout a JSON-compatible value.

    Args:
        value: JSON-compatible value to transform.
        plugin_root: Plugin root directory.
        plugin_data: Plugin data directory.
        project_dir: Optional project directory.

    Returns:
        Value with strings recursively substituted.
    """
    if isinstance(value, str):
        return substitute_string(
            value,
            plugin_root=plugin_root,
            plugin_data=plugin_data,
            project_dir=project_dir,
        )
    if isinstance(value, list):
        return [
            substitute_json(
                item,
                plugin_root=plugin_root,
                plugin_data=plugin_data,
                project_dir=project_dir,
            )
            for item in value
        ]
    if isinstance(value, dict):
        return {
            key: substitute_json(
                item,
                plugin_root=plugin_root,
                plugin_data=plugin_data,
                project_dir=project_dir,
            )
            for key, item in value.items()
        }
    return value
