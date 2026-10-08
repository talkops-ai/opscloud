"""State storage for OpsCloud plugin marketplaces, installs, and enablement."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from contextlib import contextmanager, suppress
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any, Never, cast

from opscloud.utils.logger import get_logger

from opscloud.plugins.models import (
    InstallScope,
    InstalledPluginEntry,
    MarketplaceRecord,
    MarketplaceSourceType,
    split_plugin_id,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

logger = get_logger(__name__)
_STORAGE_VERSION = 1
_INSTALLED_STORAGE_VERSION = 2
_UNVERSIONED_CACHE_KEY = "unversioned"
_CACHE_SLUG_LENGTH = 48
_CACHE_DIGEST_LENGTH = 32
SUPPORTED_MARKETPLACE_SOURCE_TYPES: frozenset[MarketplaceSourceType] = frozenset(
    {"directory", "file", "github", "git", "url"}
)


class PluginStateError(OSError):
    """Raised when existing plugin state cannot be safely modified."""


class PluginNotFoundError(ValueError):
    """Raised when an operation targets a plugin that is not installed."""


def plugin_storage_root() -> Path:
    """Return the plugin storage root directory."""
    from opscloud.config.paths import PLUGINS_DIR

    raw = os.environ.get("OPSCLOUD_PLUGIN_DIR") or os.environ.get("PLUGIN_CACHE_DIR")
    if raw:
        return Path(raw).expanduser()
    return PLUGINS_DIR


@contextmanager
def plugin_mutation_lock(*, timeout: float = -1) -> Iterator[None]:
    """Serialize plugin mutations across threads and opscloud processes.

    The lock is reentrant within one thread so compound operations such as
    marketplace removal can call the normal uninstall path while holding it.
    """
    from filelock import FileLock

    root = plugin_storage_root()
    root.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(root / ".mutation.lock"), is_singleton=True)
    with lock.acquire(timeout=timeout):
        yield


def plugin_data_dir(plugin_id: str) -> Path:
    """Return the data directory path for a plugin id without creating it."""
    return plugin_storage_root() / "data" / sanitize_plugin_id(plugin_id)


def ensure_plugin_data_dir(plugin_id: str) -> Path:
    """Return the lazily-created data directory for a plugin id."""
    data_dir = plugin_data_dir(plugin_id)
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def sanitize_plugin_id(value: str) -> str:
    """Return a bounded, collision-resistant filesystem key."""
    slug = "".join(
        ch if ch.isascii() and (ch.isalnum() or ch in {"_", "-"}) else "-"
        for ch in value
    )
    slug = slug.strip("-")[:_CACHE_SLUG_LENGTH] or "plugin"
    digest = sha256(value.encode()).hexdigest()[:_CACHE_DIGEST_LENGTH]
    return f"{slug}-{digest}"


def opaque_cache_key(value: str) -> str:
    """Return a cache key that cannot disclose source credentials."""
    return sha256(value.encode()).hexdigest()


def ensure_marketplace_cache_dir() -> Path:
    """Return the marketplace cache directory."""
    path = plugin_storage_root() / "marketplaces"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_plugin_install_cache_dir() -> Path:
    """Return the versioned plugin install cache root."""
    path = plugin_storage_root() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def versioned_cache_path(plugin_id: str, version: str | None) -> Path:
    """Return the versioned cache path for a plugin id."""
    plugin_name, marketplace = split_plugin_id(plugin_id)
    safe_version = sanitize_plugin_id(version or _UNVERSIONED_CACHE_KEY)
    return (
        ensure_plugin_install_cache_dir()
        / sanitize_plugin_id(marketplace)
        / sanitize_plugin_id(plugin_name)
        / safe_version
    )


def _state_dir() -> Path:
    from opscloud.config.paths import STATE_DIR

    return STATE_DIR


def _marketplaces_path() -> Path:
    from opscloud.config.paths import PLUGIN_MARKETPLACES_PATH

    return PLUGIN_MARKETPLACES_PATH


def _plugin_state_path() -> Path:
    from opscloud.config.paths import PLUGIN_STATE_PATH

    return PLUGIN_STATE_PATH


def _installed_plugins_path() -> Path:
    from opscloud.config.paths import PLUGIN_INSTALLED_PATH

    return PLUGIN_INSTALLED_PATH


def _user_settings_path() -> Path:
    from opscloud.config.paths import USER_SETTINGS_PATH

    return USER_SETTINGS_PATH


def _invalid_state(
    path: Path, detail: str, *, strict: bool, cause: Exception | None = None
) -> dict[str, Any]:
    msg = f"Plugin state file {path} {detail}"
    if strict:
        raise PluginStateError(msg) from cause
    logger.warning("%s", msg)
    return {}


def _load_json(
    path: Path,
    *,
    max_version: int = _STORAGE_VERSION,
    strict: bool = False,
) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _invalid_state(
            path, f"could not be read: {exc}", strict=strict, cause=exc
        )
    if not isinstance(data, dict):
        return _invalid_state(path, "is not a JSON object", strict=strict)
    version = data.get("version")
    if version is not None and (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version > max_version
    ):
        return _invalid_state(
            path, f"has unsupported version {version!r}", strict=strict
        )
    return data


def _raise_state_shape(path: Path, detail: str) -> Never:
    msg = f"Plugin state file {path} {detail}"
    raise PluginStateError(msg)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        Path(tmp_name).replace(path)
    except Exception:
        with suppress(OSError):
            Path(tmp_name).unlink()
        raise


def load_marketplace_records(
    *, project_root: Path | None = None, strict: bool = False
) -> dict[str, MarketplaceRecord]:
    """Load persisted marketplace records, plus any project-local marketplace."""
    data = _load_json(_marketplaces_path(), strict=strict)
    raw_records = data.get("marketplaces", {})
    if not isinstance(raw_records, dict):
        if strict:
            _raise_state_shape(_marketplaces_path(), "has invalid marketplaces data")
        return {}
    records: dict[str, MarketplaceRecord] = {}
    for name, record in raw_records.items():
        if not isinstance(name, str) or not isinstance(record, dict):
            continue
        source_type = record.get("source_type")
        source = record.get("source")
        if (
            not isinstance(source_type, str)
            or source_type not in SUPPORTED_MARKETPLACE_SOURCE_TYPES
            or not isinstance(source, str)
            or not isinstance(record.get("install_location", source), str)
        ):
            logger.debug("Skipping unsupported marketplace record %r", name)
            continue
        ref = record.get("ref")
        is_default = bool(record.get("is_default", False))
        plugin_count = int(record.get("plugin_count", 0))
        records[name] = MarketplaceRecord(
            name=name,
            source_type=cast(MarketplaceSourceType, source_type),
            source=source,
            install_location=record.get("install_location", source),
            ref=ref if isinstance(ref, str) else None,
            plugin_count=plugin_count,
            is_default=is_default,
        )

    # Seed default marketplaces if missing and not explicitly removed by the user
    from opscloud.config.plugins import DEFAULT_MARKETPLACE_CONFIGS

    removed_defaults = set(data.get("removed_defaults", []))
    for default_cfg in DEFAULT_MARKETPLACE_CONFIGS:
        if default_cfg.name not in records and default_cfg.name not in removed_defaults:
            default_dir = ensure_marketplace_cache_dir() / default_cfg.name
            records[default_cfg.name] = MarketplaceRecord(
                name=default_cfg.name,
                source_type="git",
                source=default_cfg.repository_url,
                install_location=str(default_dir),
                ref=default_cfg.ref,
                is_default=True,
            )

    # Auto-detect project marketplace record if available
    target_root = project_root
    if target_root is None:
        try:
            cwd = Path.cwd()
            if (
                (cwd / ".opscloud").is_dir()
                or (cwd / ".opscode").is_dir()
                or (cwd / ".claude-plugin").is_dir()
                or (cwd / "marketplace.json").is_file()
            ):
                target_root = cwd
        except (OSError, RuntimeError):
            pass

    if target_root is not None:
        try:
            from opscloud.plugins.marketplace import (
                _load_marketplace_file,
                find_marketplace_manifest,
            )

            manifest_path = (
                find_marketplace_manifest(target_root / ".opscloud")
                or find_marketplace_manifest(target_root / ".opscode")
                or find_marketplace_manifest(target_root)
            )
            if manifest_path is not None:
                mp = _load_marketplace_file(manifest_path)
                if mp.name not in records:
                    records[mp.name] = MarketplaceRecord(
                        name=mp.name,
                        source_type="file",
                        source=str(manifest_path),
                        install_location=str(manifest_path),
                    )
        except Exception as exc:
            logger.debug("Could not auto-discover project marketplace record: %s", exc)

    return records


def save_marketplace_record(record: MarketplaceRecord) -> None:
    """Persist a marketplace record."""
    data = _load_json(_marketplaces_path(), strict=True)
    marketplaces = data.get("marketplaces")
    if marketplaces is None:
        marketplaces = {}
    elif not isinstance(marketplaces, dict):
        _raise_state_shape(_marketplaces_path(), "has invalid marketplaces data")
    entry_dict: dict[str, Any] = {
        "install_location": record.install_location,
        "source_type": record.source_type,
        "source": record.source,
    }
    from opscloud.config.plugins import is_default_marketplace

    if record.ref:
        entry_dict["ref"] = record.ref
    if record.plugin_count:
        entry_dict["plugin_count"] = record.plugin_count
    if record.is_default or is_default_marketplace(record.name):
        entry_dict["is_default"] = True
    marketplaces[record.name] = entry_dict

    payload: dict[str, Any] = {
        "version": _STORAGE_VERSION,
        "marketplaces": marketplaces,
    }
    removed_defaults = [d for d in data.get("removed_defaults", []) if d != record.name]
    if removed_defaults:
        payload["removed_defaults"] = removed_defaults

    _atomic_write_json(_marketplaces_path(), payload)


def remove_marketplace_record(name: str) -> bool:
    """Remove a marketplace record."""
    data = _load_json(_marketplaces_path(), strict=True)
    marketplaces = data.get("marketplaces")
    if marketplaces is None:
        marketplaces = {}
    elif not isinstance(marketplaces, dict):
        _raise_state_shape(_marketplaces_path(), "has invalid marketplaces data")

    from opscloud.config.plugins import is_default_marketplace

    if name not in marketplaces and not is_default_marketplace(name):
        return False
    removed_record = marketplaces.pop(name, None)
    payload: dict[str, Any] = {
        "version": _STORAGE_VERSION,
        "marketplaces": marketplaces,
    }
    is_default = (
        is_default_marketplace(name)
        or (isinstance(removed_record, dict) and removed_record.get("is_default", False))
    )
    if is_default:
        existing_removed = data.get("removed_defaults", [])
        if name not in existing_removed:
            payload["removed_defaults"] = [*existing_removed, name]
        else:
            payload["removed_defaults"] = existing_removed
    elif "removed_defaults" in data:
        payload["removed_defaults"] = data["removed_defaults"]

    _atomic_write_json(_marketplaces_path(), payload)
    return True


# ── Scoped Settings / Enablement ──────────────────────────────


def _load_settings_enabled_plugins(path: Path) -> frozenset[str]:
    """Read enabled plugins (where value is True) from a settings JSON file."""
    m = _load_settings_enabled_plugins_map(path)
    return frozenset(k for k, v in m.items() if v is True)


def _load_settings_enabled_plugins_map(path: Path) -> dict[str, bool]:
    """Read ``enabledPlugins`` map from a settings JSON file."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("Could not read settings file %s", path)
        return {}
    if not isinstance(data, dict):
        return {}
    enabled = data.get("enabledPlugins", {})
    if not isinstance(enabled, dict):
        return {}
    return {
        key: value
        for key, value in enabled.items()
        if isinstance(key, str) and isinstance(value, bool)
    }


def _load_settings_disabled_plugins(path: Path) -> frozenset[str]:
    """Read explicitly disabled plugins (value is False) from settings JSON."""
    m = _load_settings_enabled_plugins_map(path)
    return frozenset(k for k, v in m.items() if v is False)


def load_all_disabled_plugin_ids(
    project_root: Path | None = None,
) -> frozenset[str]:
    """Merged union of user + project + local explicitly disabled plugin ids."""
    from opscloud.config.settings import settings

    effective_root = (
        project_root if isinstance(project_root, Path) else settings.effective_project_root
    )
    result = set(_load_settings_disabled_plugins(_user_settings_path()))
    if isinstance(effective_root, Path):
        from opscloud.config.paths import (
            project_local_settings_path,
            project_settings_path,
        )

        result |= set(
            _load_settings_disabled_plugins(project_settings_path(effective_root))
        )
        result |= set(
            _load_settings_disabled_plugins(project_local_settings_path(effective_root))
        )
    return frozenset(result)


def _write_settings_enabled_plugins(path: Path, enabled_ids: set[str]) -> None:
    """Write ``enabledPlugins`` to a settings JSON file, preserving other keys."""
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        if not isinstance(existing, dict):
            existing = {}
    existing["enabledPlugins"] = dict.fromkeys(sorted(enabled_ids), True)
    _atomic_write_json(path, existing)


def _write_settings_plugin_enabled_state(
    path: Path, plugin_id: str, enabled: bool
) -> None:
    """Set a single plugin's enabled boolean state in a settings JSON file."""
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        if not isinstance(existing, dict):
            existing = {}

    current_map: dict[str, Any] = existing.get("enabledPlugins", {})
    if not isinstance(current_map, dict):
        current_map = {}

    current_map[plugin_id] = enabled
    existing["enabledPlugins"] = current_map
    _atomic_write_json(path, existing)


def _remove_settings_plugin_enabled_state(path: Path, plugin_id: str) -> None:
    """Remove a plugin from the enabledPlugins map in a settings JSON file."""
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(data, dict):
        return
    enabled = data.get("enabledPlugins")
    if isinstance(enabled, dict) and plugin_id in enabled:
        del enabled[plugin_id]
        data["enabledPlugins"] = enabled
        _atomic_write_json(path, data)


def load_user_enabled_plugin_ids() -> frozenset[str]:
    """Read enabledPlugins from ``~/.opscloud/settings.json``."""
    return _load_settings_enabled_plugins(_user_settings_path())


def load_project_enabled_plugin_ids(project_root: Path) -> frozenset[str]:
    """Read enabledPlugins from ``{project}/.opscloud/settings.json``."""
    if not isinstance(project_root, Path):
        return frozenset()
    from opscloud.config.paths import project_settings_path

    return _load_settings_enabled_plugins(project_settings_path(project_root))


def load_local_enabled_plugin_ids(project_root: Path) -> frozenset[str]:
    """Read enabledPlugins from ``{project}/.opscloud/settings.local.json``."""
    if not isinstance(project_root, Path):
        return frozenset()
    from opscloud.config.paths import project_local_settings_path

    return _load_settings_enabled_plugins(project_local_settings_path(project_root))


def load_all_enabled_plugin_ids(
    project_root: Path | None = None,
) -> frozenset[str]:
    """Merged union of user + project + local enabled plugin ids."""
    from opscloud.config.settings import settings

    effective_root = (
        project_root if isinstance(project_root, Path) else settings.effective_project_root
    )
    result = set(load_user_enabled_plugin_ids())
    if isinstance(effective_root, Path):
        result |= load_project_enabled_plugin_ids(effective_root)
        result |= load_local_enabled_plugin_ids(effective_root)
    return frozenset(result)


def load_enabled_plugin_ids(*, strict: bool = False) -> frozenset[str]:
    """Load enabled plugin ids (user scope backward-compatible entry point)."""
    return load_user_enabled_plugin_ids()


def set_plugin_enabled_for_scope(
    plugin_id: str,
    enabled: bool,
    scope: InstallScope = "user",
    project_root: Path | None = None,
) -> None:
    """Write enablement to the correct settings file for the given scope."""
    from opscloud.config.paths import (
        project_local_settings_path,
        project_settings_path,
    )

    if scope in {"user", "global"}:
        path = _user_settings_path()
    elif scope == "project":
        if project_root is None:
            raise ValueError("project_root required for project scope")
        path = project_settings_path(project_root)
    elif scope == "local":
        if project_root is None:
            raise ValueError("project_root required for local scope")
        path = project_local_settings_path(project_root)
        _ensure_local_settings_gitignored(project_root)
    else:
        path = _user_settings_path()

    _write_settings_plugin_enabled_state(path, plugin_id, enabled)


def set_plugin_enabled(plugin_id: str, enabled: bool) -> None:
    """Persist a plugin enablement value (user scope)."""
    set_plugin_enabled_for_scope(plugin_id, enabled, scope="user")


def remove_plugin_enabled_for_scope(
    plugin_id: str,
    scope: InstallScope = "user",
    project_root: Path | None = None,
) -> None:
    """Remove plugin enablement key from the settings file for the given scope."""
    from opscloud.config.paths import (
        project_local_settings_path,
        project_settings_path,
    )

    if scope in {"user", "global"}:
        path = _user_settings_path()
    elif scope == "project":
        if project_root is None:
            return
        path = project_settings_path(project_root)
    elif scope == "local":
        if project_root is None:
            return
        path = project_local_settings_path(project_root)
    else:
        path = _user_settings_path()

    _remove_settings_plugin_enabled_state(path, plugin_id)


def _ensure_local_settings_gitignored(project_root: Path) -> None:
    """Add ``settings.local.json`` to git excludes."""
    pattern = "**/.opscloud/settings.local.json"

    # Project-local git exclude
    git_info_exclude = project_root / ".git" / "info" / "exclude"
    if git_info_exclude.parent.is_dir():
        try:
            content = (
                git_info_exclude.read_text(encoding="utf-8")
                if git_info_exclude.exists()
                else ""
            )
            if pattern not in content:
                git_info_exclude.parent.mkdir(parents=True, exist_ok=True)
                with git_info_exclude.open("a", encoding="utf-8") as f:
                    f.write(f"\n{pattern}\n")
        except OSError:
            logger.debug("Could not add project git exclude for %s", pattern)


# ── Installed Plugins Registry ────────────────────────────────


def _parse_installed_plugin_json_entry(
    persisted_entry: object,
) -> InstalledPluginEntry | None:
    """Parse a single install entry from persisted JSON."""
    if not isinstance(persisted_entry, dict):
        return None
    install_path = persisted_entry.get("installPath") or persisted_entry.get(
        "install_path"
    )
    version = persisted_entry.get("version")
    if (
        not isinstance(install_path, str)
        or not install_path
        or (version is not None and (not isinstance(version, str) or not version))
    ):
        return None
    scope_raw = persisted_entry.get("scope", "user")
    scope: InstallScope = (
        cast(InstallScope, scope_raw)
        if scope_raw in {"user", "project", "local", "global"}
        else "user"
    )
    project_path = persisted_entry.get("projectPath")
    installed_at = persisted_entry.get("installedAt")
    last_updated = persisted_entry.get("lastUpdated")
    git_commit_sha = persisted_entry.get("gitCommitSha")
    return InstalledPluginEntry(
        install_path=install_path,
        version=version if isinstance(version, str) else None,
        scope=scope,
        project_path=project_path if isinstance(project_path, str) else None,
        installed_at=installed_at if isinstance(installed_at, str) else None,
        last_updated=last_updated if isinstance(last_updated, str) else None,
        git_commit_sha=git_commit_sha if isinstance(git_commit_sha, str) else None,
    )


def load_installed_plugin_entries(
    *, strict: bool = False
) -> dict[str, list[InstalledPluginEntry]]:
    """Load installed plugin records as arrays per plugin ID."""
    data = _load_json(
        _installed_plugins_path(),
        max_version=_INSTALLED_STORAGE_VERSION,
        strict=strict,
    )
    raw_plugins = data.get("plugins", {})
    if not isinstance(raw_plugins, dict):
        if strict:
            _raise_state_shape(_installed_plugins_path(), "has invalid plugins data")
        return {}
    result: dict[str, list[InstalledPluginEntry]] = {}
    for plugin_id, entries in raw_plugins.items():
        if not isinstance(plugin_id, str) or not isinstance(entries, list):
            if strict:
                _raise_state_shape(
                    _installed_plugins_path(), "has malformed plugin entries"
                )
            continue
        parsed_entries = [
            entry
            for item in entries
            if (entry := _parse_installed_plugin_json_entry(item)) is not None
        ]
        if parsed_entries:
            result[plugin_id] = parsed_entries
        elif strict:
            _raise_state_shape(
                _installed_plugins_path(), f"has malformed entry for {plugin_id!r}"
            )
    return result


def load_installed_plugins(
    *,
    strict: bool = False,
    project_root: Path | None = None,
) -> dict[str, InstalledPluginEntry]:
    """Load installed plugin records (first entry per plugin, backward-compat).

    If project_root is provided, entries matching that workspace or user scope are prioritized.
    """
    all_entries = load_installed_plugin_entries(strict=strict)
    result: dict[str, InstalledPluginEntry] = {}
    for plugin_id, entries in all_entries.items():
        if not entries:
            continue
        primary: InstalledPluginEntry | None = None
        if project_root is not None:
            for e in entries:
                if e.project_path and (
                    e.project_path == str(project_root)
                    or Path(e.project_path).resolve() == project_root.resolve()
                ):
                    primary = e
                    break
            if primary is None:
                for e in entries:
                    if e.scope in {"user", "global"}:
                        primary = e
                        break
        if primary is None:
            primary = entries[0]
        result[plugin_id] = primary
    return result


def _entry_to_json(entry: InstalledPluginEntry) -> dict[str, Any]:
    """Serialize an install entry to JSON (Claude Code camelCase keys)."""
    payload: dict[str, Any] = {
        "scope": entry.scope,
        "installPath": entry.install_path,
    }
    if entry.project_path is not None:
        payload["projectPath"] = entry.project_path
    if entry.version is not None:
        payload["version"] = entry.version
    if entry.installed_at is not None:
        payload["installedAt"] = entry.installed_at
    if entry.last_updated is not None:
        payload["lastUpdated"] = entry.last_updated
    if entry.git_commit_sha is not None:
        payload["gitCommitSha"] = entry.git_commit_sha
    return payload


def _write_installed_plugins_raw(
    plugins: dict[str, list[InstalledPluginEntry]],
) -> None:
    """Write the full installed-plugins registry (array-per-plugin)."""
    _atomic_write_json(
        _installed_plugins_path(),
        {
            "version": _INSTALLED_STORAGE_VERSION,
            "plugins": {
                plugin_id: [_entry_to_json(e) for e in entries]
                for plugin_id, entries in sorted(plugins.items())
            },
        },
    )


def _write_installed_plugins(
    plugins: dict[str, InstalledPluginEntry],
) -> None:
    """Write installed plugins (single-entry compat wrapper)."""
    _write_installed_plugins_raw(
        {pid: [entry] for pid, entry in plugins.items()}
    )


def _now_iso() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    from datetime import datetime, timezone

    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def add_installed_plugin(
    plugin_id: str,
    *,
    install_path: str,
    version: str | None = None,
    scope: InstallScope = "user",
    project_root: Path | None = None,
    git_commit_sha: str | None = None,
) -> InstalledPluginEntry:
    """Add a scoped install entry for ``plugin_id``."""
    all_entries = load_installed_plugin_entries(strict=True)
    now = _now_iso()
    project_path = str(project_root) if project_root is not None else None

    entry = InstalledPluginEntry(
        install_path=install_path,
        version=version,
        scope=scope,
        project_path=project_path,
        installed_at=now,
        last_updated=now,
        git_commit_sha=git_commit_sha,
    )

    existing = all_entries.get(plugin_id, [])
    updated = [
        e
        for e in existing
        if not (e.scope == scope and e.project_path == project_path)
    ]
    updated.append(entry)
    all_entries[plugin_id] = updated
    _write_installed_plugins_raw(all_entries)
    return entry


def get_primary_install_entry(plugin_id: str) -> InstalledPluginEntry | None:
    """Return the first install entry for a plugin id."""
    return load_installed_plugins().get(plugin_id)


def remove_installed_plugin(
    plugin_id: str,
    *,
    scope: InstallScope | None = None,
    project_root: Path | None = None,
    project_path: str | None = None,
) -> InstalledPluginEntry | None:
    """Remove install record(s) for a plugin."""
    all_entries = load_installed_plugin_entries(strict=True)
    target_id = plugin_id
    if target_id not in all_entries and "@" not in target_id:
        matches = [pid for pid in all_entries if pid.startswith(f"{target_id}@")]
        if matches:
            target_id = matches[0]

    existing = all_entries.get(target_id)
    if not existing:
        return None

    target_project_path = (
        project_path
        if project_path is not None
        else (str(project_root) if (scope != "user" and project_root is not None) else None)
    )

    removed: InstalledPluginEntry | None = None
    remaining: list[InstalledPluginEntry] = []

    if scope is None:
        # Global removal: remove all entries for this plugin
        removed = existing[0] if existing else None
        all_entries.pop(target_id, None)
    else:
        # Pass 1: exact match on scope and project_path
        for e in existing:
            is_match = False
            if removed is None and e.scope == scope:
                if scope in {"user", "global"}:
                    is_match = True
                elif target_project_path is None:
                    is_match = True
                else:
                    if e.project_path == target_project_path:
                        is_match = True
                    elif e.project_path:
                        try:
                            if Path(e.project_path).resolve() == Path(target_project_path).resolve():
                                is_match = True
                        except (OSError, RuntimeError):
                            pass
            if is_match:
                removed = e
            else:
                remaining.append(e)

        # Fallback 1: match on scope even if project_path differed (cross-project invocation)
        if removed is None:
            for idx, e in enumerate(remaining):
                if e.scope == scope:
                    removed = remaining.pop(idx)
                    break

        # Fallback 2: if there's only one entry or scope was generic, remove it rather than no-op
        if removed is None and remaining:
            removed = remaining.pop(0)

        if remaining:
            all_entries[target_id] = remaining
        else:
            all_entries.pop(target_id, None)

    _write_installed_plugins_raw(all_entries)
    return removed


def cache_and_register_plugin(
    plugin_id: str,
    source_dir: Path,
    *,
    version: str | None = None,
    scope: InstallScope = "user",
    project_root: Path | None = None,
    validate: Callable[[Path], None] | None = None,
) -> Path:
    """Copy a plugin into the versioned cache and register the install."""
    source = source_dir.resolve()
    if not source.is_dir():
        msg = f"Plugin source directory not found: {source}"
        raise FileNotFoundError(msg)

    cache_path = versioned_cache_path(plugin_id, version)
    if cache_path.exists() and version is not None:
        try:
            if any(cache_path.iterdir()):
                add_installed_plugin(
                    plugin_id,
                    install_path=str(cache_path),
                    version=version,
                    scope=scope,
                    project_root=project_root,
                )
                return cache_path
        except OSError:
            pass

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = cache_path.parent / f".{cache_path.name}.tmp-{os.getpid()}"
    backup_dir = cache_path.parent / f".{cache_path.name}.backup-{os.getpid()}"
    if temp_dir.exists():
        shutil.rmtree(temp_dir, ignore_errors=True)
    if backup_dir.exists():
        shutil.rmtree(backup_dir, ignore_errors=True)
    try:
        shutil.copytree(source, temp_dir, symlinks=True, dirs_exist_ok=False)
        git_dir = temp_dir / ".git"
        if git_dir.exists():
            shutil.rmtree(git_dir, ignore_errors=True)
        if validate is not None:
            validate(temp_dir)
        if cache_path.exists():
            cache_path.replace(backup_dir)
        try:
            temp_dir.replace(cache_path)
        except OSError:
            if backup_dir.exists() and not cache_path.exists():
                backup_dir.replace(cache_path)
            raise
        shutil.rmtree(backup_dir, ignore_errors=True)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    add_installed_plugin(
        plugin_id,
        install_path=str(cache_path.resolve()),
        version=version,
        scope=scope,
        project_root=project_root,
    )
    return cache_path.resolve()


def uninstall_plugin(
    plugin_id: str,
    *,
    scope: InstallScope | None = None,
    project_root: Path | None = None,
    project_path: str | None = None,
) -> bool:
    """Disable a plugin, remove install records, and delete orphaned cache dirs.

    Returns:
        True on successful uninstallation.

    Raises:
        PluginNotFoundError: If the plugin is not installed in the target scope.
    """
    from opscloud.config.settings import settings
    from opscloud.skills.registry import SkillRegistry

    target_id = plugin_id
    all_entries = load_installed_plugin_entries(strict=True)
    if target_id not in all_entries and "@" not in target_id:
        matches = [pid for pid in all_entries if pid.startswith(f"{target_id}@")]
        if matches:
            target_id = matches[0]

    effective_root = (
        Path(project_path)
        if project_path is not None
        else (project_root if isinstance(project_root, Path) else settings.effective_project_root)
    )

    removed = remove_installed_plugin(
        target_id, scope=scope, project_root=effective_root, project_path=project_path
    )

    if removed is None:
        scope_detail = f" (scope={scope!r})" if scope else ""
        raise PluginNotFoundError(f"Plugin {plugin_id!r} is not installed{scope_detail}")

    entry_scope = removed.scope
    entry_root = Path(removed.project_path) if removed.project_path else effective_root

    # Clean up enablement in the removed entry's scope
    with suppress(Exception):
        set_plugin_enabled_for_scope(
            target_id, False, scope=entry_scope, project_root=entry_root
        )
        remove_plugin_enabled_for_scope(
            target_id, scope=entry_scope, project_root=entry_root
        )

    if scope is not None and scope != entry_scope:
        with suppress(Exception):
            set_plugin_enabled_for_scope(
                target_id, False, scope=scope, project_root=effective_root
            )
            remove_plugin_enabled_for_scope(
                target_id, scope=scope, project_root=effective_root
            )

    all_after = load_installed_plugin_entries()
    has_remaining_installs = bool(all_after.get(target_id))

    if scope is None or not has_remaining_installs:
        with suppress(Exception):
            set_plugin_enabled_for_scope(target_id, False, scope="user")
            remove_plugin_enabled_for_scope(target_id, scope="user")
        if effective_root:
            with suppress(Exception):
                set_plugin_enabled_for_scope(
                    target_id, False, scope="project", project_root=effective_root
                )
                remove_plugin_enabled_for_scope(
                    target_id, scope="project", project_root=effective_root
                )
            with suppress(Exception):
                set_plugin_enabled_for_scope(
                    target_id, False, scope="local", project_root=effective_root
                )
                remove_plugin_enabled_for_scope(
                    target_id, scope="local", project_root=effective_root
                )
        if entry_root and entry_root != effective_root:
            with suppress(Exception):
                set_plugin_enabled_for_scope(
                    target_id, False, scope="project", project_root=entry_root
                )
                remove_plugin_enabled_for_scope(
                    target_id, scope="project", project_root=entry_root
                )
            with suppress(Exception):
                set_plugin_enabled_for_scope(
                    target_id, False, scope="local", project_root=entry_root
                )
                remove_plugin_enabled_for_scope(
                    target_id, scope="local", project_root=entry_root
                )

    all_paths_after = {
        e.install_path
        for entries in all_after.values()
        for e in entries
    }
    if removed.install_path not in all_paths_after:
        path = Path(removed.install_path)
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)

    with suppress(Exception):
        SkillRegistry.get_instance().discover_skills(force=True)

    return True
