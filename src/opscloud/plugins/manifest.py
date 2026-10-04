"""Plugin manifest parsing and component inventory building for K8s Autopilot."""

from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath
import re
from typing import Any

from opscloud.plugins.models import (
    ComponentInventory,
    JsonObject,
    PluginManifest,
    UnsupportedComponent,
)
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

_MANIFEST_RELATIVE_PATHS = (
    Path(".claude-plugin") / "plugin.json",
    Path(".opscode-plugin") / "plugin.json",
    Path(".codex-plugin") / "plugin.json",
    Path("plugin.json"),
)
_PATH_COMPONENT_FIELDS = {"skills", "mcpServers", "agents", "commands"}
_UNSUPPORTED_COMPONENT_DIRS: tuple[UnsupportedComponent, ...] = ("hooks",)
_NAME_RE = re.compile(r"^[^\s]+$")


class PluginManifestError(ValueError):
    """Raised when a plugin manifest is malformed enough to skip the plugin."""


def find_manifest_path(root: Path) -> Path | None:
    """Return the first supported manifest path under `root`, if present."""
    for rel in _MANIFEST_RELATIVE_PATHS:
        path = root / rel
        try:
            if path.is_file():
                return path
        except OSError:
            logger.warning("Could not inspect plugin manifest path %s", path)
    return None


def _validate_name(name: object, *, fallback: str | None = None, allow_at: bool = True) -> str:
    if isinstance(name, str) and name and _NAME_RE.fullmatch(name) and (allow_at or "@" not in name):
        return name
    if fallback and _NAME_RE.fullmatch(fallback) and (allow_at or "@" not in fallback):
        return fallback
    msg = f"Invalid plugin name: {name!r}"
    raise PluginManifestError(msg)


def _is_windows_absolute(path: str) -> bool:
    return bool(PureWindowsPath(path).drive or PureWindowsPath(path).root)


def _resolve_component_path(
    declaration: str,
    plugin_root: Path,
    field_name: str,
    warnings: list[str],
) -> Path | None:
    if not declaration.startswith("./"):
        warnings.append(f"ignoring {field_name}: path must start with './' relative to plugin root")
        return None
    relative = declaration[2:]
    if not relative:
        warnings.append(f"ignoring {field_name}: path must not be './'")
        return None
    path = Path(relative)
    if any(part == ".." for part in path.parts):
        warnings.append(f"ignoring {field_name}: path must not contain '..'")
        return None
    if path.is_absolute() or _is_windows_absolute(relative):
        warnings.append(f"ignoring {field_name}: path must stay within the plugin root")
        return None
    try:
        root_resolved = plugin_root.resolve()
        resolved = (plugin_root / path).resolve()
    except OSError as exc:
        warnings.append(f"ignoring {field_name}: could not resolve {declaration!r}: {exc}")
        return None
    if not resolved.is_relative_to(root_resolved):
        warnings.append(f"ignoring {field_name}: path escapes plugin root")
        return None
    return resolved


def _resolve_component_paths(
    declaration: object,
    plugin_root: Path,
    field_name: str,
    warnings: list[str],
) -> tuple[Path, ...]:
    raw_paths: list[str]
    if isinstance(declaration, str):
        raw_paths = [declaration]
    elif isinstance(declaration, list):
        raw_paths = [item for item in declaration if isinstance(item, str)]
        warnings.extend(
            f"ignoring {field_name}: expected path string, got {type(item).__name__}"
            for item in declaration
            if not isinstance(item, str)
        )
    else:
        warnings.append(f"ignoring {field_name}: expected path string or list of strings")
        return ()
    paths: list[Path] = []
    for raw_path in raw_paths:
        resolved = _resolve_component_path(raw_path, plugin_root, field_name, warnings)
        if resolved is not None:
            paths.append(resolved)
    return tuple(paths)


def _inline_mcp(value: object) -> JsonObject:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, list):
        merged: JsonObject = {}
        for item in value:
            if isinstance(item, dict):
                merged.update(item)
        return merged
    return {}


def load_manifest(
    root: Path, *, fallback_name: str | None = None
) -> tuple[PluginManifest | None, Path | None, tuple[str, ...]]:
    """Load a plugin manifest from known locations."""
    manifest_path = find_manifest_path(root)
    if manifest_path is None:
        return None, None, ()
    try:
        decoded = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        msg = f"Invalid JSON syntax in {manifest_path}: {exc}"
        raise PluginManifestError(msg) from exc
    except OSError as exc:
        msg = f"Could not read plugin manifest {manifest_path}: {exc}"
        raise PluginManifestError(msg) from exc
    if not isinstance(decoded, dict):
        msg = f"Plugin manifest {manifest_path} must be a JSON object"
        raise PluginManifestError(msg)
    raw = decoded

    warnings: list[str] = []
    name = _validate_name(raw.get("name"), fallback=fallback_name)
    component_paths: dict[str, tuple[Path, ...]] = {}
    for field_name in _PATH_COMPONENT_FIELDS:
        declaration = raw.get(field_name)
        if declaration is None:
            continue
        if field_name == "mcpServers" and isinstance(declaration, dict):
            continue
        paths = _resolve_component_paths(declaration, root, field_name, warnings)
        if paths:
            component_paths[field_name] = paths

    version_value = raw.get("version")
    version = version_value if isinstance(version_value, str) else "1.0.0"
    display_name_value = raw.get("displayName") or raw.get("display_name")
    description_value = raw.get("description")
    author_value = raw.get("author")

    skills_dir = root / "skills" if (root / "skills").is_dir() else None
    agents_dir = root / "agents" if (root / "agents").is_dir() else None

    manifest = PluginManifest(
        name=name,
        version=version,
        component_paths=component_paths,
        inline_mcp=_inline_mcp(raw.get("mcpServers")),
        display_name=(display_name_value if isinstance(display_name_value, str) else None),
        description=(description_value if isinstance(description_value, str) else None),
        author=author_value,
        skills_dir=skills_dir,
        agents_dir=agents_dir,
    )
    return manifest, manifest_path, tuple(warnings)


def _existing_component_path(path: Path, plugin_root: Path) -> tuple[Path, ...]:
    try:
        if not path.exists():
            return ()
        resolved = path.resolve()
        if not resolved.is_relative_to(plugin_root.resolve()):
            logger.warning("Ignoring plugin component outside plugin root: %s", path)
            return ()
    except OSError:
        logger.warning("Could not inspect plugin component path %s", path)
        return ()
    else:
        return (resolved,)


def _unsupported_component_dirs(
    plugin_root: Path,
) -> tuple[UnsupportedComponent, ...]:
    found: list[UnsupportedComponent] = []
    for name in _UNSUPPORTED_COMPONENT_DIRS:
        path = plugin_root / name
        try:
            if path.is_dir():
                found.append(name)
        except OSError:
            logger.warning("Could not inspect plugin component path %s", path)
    return tuple(found)


def build_inventory(
    plugin_root: Path,
    manifest: PluginManifest | None,
    manifest_warnings: tuple[str, ...] = (),
) -> ComponentInventory:
    """Build component inventory for a plugin."""
    plugin_root = plugin_root.resolve()
    warnings = list(manifest_warnings)
    metadata_paths = manifest.component_paths if manifest else {}

    default_skills = _existing_component_path(plugin_root / "skills", plugin_root)
    root_skill = (
        ()
        if default_skills or (manifest and "skills" in manifest.component_paths)
        else _existing_component_path(plugin_root / "SKILL.md", plugin_root)
    )
    skills = (*default_skills, *metadata_paths.get("skills", ()), *root_skill)

    default_agents = _existing_component_path(plugin_root / "agents", plugin_root)
    agents = (*default_agents, *metadata_paths.get("agents", ()))

    default_commands = _existing_component_path(plugin_root / "commands", plugin_root)
    commands = (*default_commands, *metadata_paths.get("commands", ()))

    mcp_files = (
        *_existing_component_path(plugin_root / ".mcp.json", plugin_root),
        *metadata_paths.get("mcpServers", ()),
    )

    unsupported = _unsupported_component_dirs(plugin_root)

    return ComponentInventory(
        skills=tuple(dict.fromkeys(skills)),
        mcp_files=tuple(dict.fromkeys(mcp_files)),
        agents=tuple(dict.fromkeys(agents)),
        commands=tuple(dict.fromkeys(commands)),
        unsupported=unsupported,
        warnings=tuple(warnings),
    )


def inspect_plugin_components(
    plugin_root: Path,
    manifest: PluginManifest | None = None,
) -> dict[str, Any]:
    """Inspect a plugin filesystem tree and return exact component names and metadata."""
    plugin_root = plugin_root.resolve()

    if manifest is None:
        try:
            manifest, _, _ = load_manifest(plugin_root, fallback_name=plugin_root.name)
        except Exception:
            manifest = None

    # 1. Skills
    skill_names: list[str] = []
    skills_dir = plugin_root / "skills"
    if skills_dir.is_dir():
        for item in sorted(skills_dir.iterdir()):
            if item.is_dir() and (item / "SKILL.md").is_file():
                skill_names.append(item.name)
            elif item.is_file() and item.suffix == ".md" and item.name != "SKILL.md":
                skill_names.append(item.stem)
        if not skill_names and (skills_dir / "SKILL.md").is_file():
            skill_names.append(plugin_root.name)
    elif (plugin_root / "SKILL.md").is_file():
        skill_names.append(plugin_root.name)

    # 2. MCP Servers
    mcp_server_names: list[str] = []
    mcp_file = plugin_root / ".mcp.json"
    if mcp_file.is_file():
        try:
            data = json.loads(mcp_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                servers = data.get("mcpServers")
                if isinstance(servers, dict):
                    mcp_server_names.extend(servers.keys())
                elif "mcpServers" not in data:
                    mcp_server_names.extend(data.keys())
        except Exception:
            pass

    if manifest and manifest.inline_mcp:
        for k in manifest.inline_mcp:
            if k not in mcp_server_names:
                mcp_server_names.append(k)

    # 3. Commands
    command_names: list[str] = []
    commands_dir = plugin_root / "commands"
    if commands_dir.is_dir():
        for item in sorted(commands_dir.glob("*.md")):
            command_names.append(item.stem)

    # 4. Author string normalization
    raw_author = manifest.author if manifest else None
    author_str = None
    if isinstance(raw_author, dict):
        author_str = raw_author.get("name")
    elif isinstance(raw_author, str):
        if raw_author.strip().startswith("{"):
            try:
                parsed = json.loads(raw_author)
                author_str = parsed.get("name") if isinstance(parsed, dict) else raw_author
            except Exception:
                author_str = raw_author
        else:
            author_str = raw_author

    return {
        "display_name": manifest.display_name if manifest and manifest.display_name else None,
        "description": manifest.description if manifest and manifest.description else None,
        "version": manifest.version if manifest and manifest.version else "1.0.0",
        "author": author_str,
        "skill_names": skill_names,
        "skill_count": len(skill_names),
        "mcp_server_names": mcp_server_names,
        "command_names": command_names,
    }
