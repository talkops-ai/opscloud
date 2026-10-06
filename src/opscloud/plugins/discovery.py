"""Plugin discovery, install, and enablement helpers for OpsCloud."""

from __future__ import annotations

import shutil
from functools import partial
from pathlib import Path
from typing import Any

from opscloud.utils.logger import get_logger

from opscloud.plugins.manifest import (
    PluginManifestError,
    build_inventory,
    load_manifest,
)
from opscloud.plugins.marketplace import (
    MarketplaceError,
    load_marketplace,
    load_marketplace_location,
    materialize_marketplace_source,
    materialize_plugin_source,
    parse_marketplace_source,
    redact_urls_in_text,
)
from opscloud.plugins.models import (
    InstallScope,
    MarketplacePluginEntry,
    MarketplaceRecord,
    PluginDiscoveryResult,
    PluginInstance,
    PluginMarketplace,
    RepositoryMarketplaceSource,
    split_plugin_id,
)
from opscloud.plugins.store import (
    cache_and_register_plugin,
    ensure_marketplace_cache_dir,
    ensure_plugin_data_dir,
    get_primary_install_entry,
    load_all_enabled_plugin_ids,
    load_enabled_plugin_ids,
    load_local_enabled_plugin_ids,
    load_project_enabled_plugin_ids,
    load_installed_plugins,
    load_marketplace_records,
    plugin_data_dir,
    plugin_mutation_lock,
    remove_marketplace_record,
    save_marketplace_record,
    set_plugin_enabled,
    set_plugin_enabled_for_scope,
    uninstall_plugin as uninstall_plugin_record,
)

logger = get_logger(__name__)


@plugin_mutation_lock()
def add_local_marketplace(path: str | Path) -> PluginMarketplace:
    """Add a local marketplace to OpsCloud state."""
    marketplace = load_marketplace(Path(path))
    save_marketplace_record(
        MarketplaceRecord(
            name=marketplace.name,
            source_type="directory",
            source=str(marketplace.root),
            install_location=str(marketplace.root),
        )
    )
    return marketplace


@plugin_mutation_lock()
def add_marketplace_source(raw: str) -> PluginMarketplace:
    """Add a marketplace from a pasted source string."""
    source = parse_marketplace_source(raw)
    marketplace, location = materialize_marketplace_source(source)
    save_marketplace_record(
        MarketplaceRecord(
            name=marketplace.name,
            source_type=source.source_type,
            source=source.value,
            install_location=str(location),
            ref=source.ref if isinstance(source, RepositoryMarketplaceSource) else None,
        )
    )
    return marketplace


@plugin_mutation_lock()
def remove_marketplace(name: str) -> bool:
    """Remove a marketplace and every plugin installed from it."""
    record = load_marketplace_records().get(name)
    if record is None:
        return False

    installed = load_installed_plugins(strict=True)
    enabled = load_enabled_plugin_ids(strict=True)
    plugin_ids = set(installed) | set(enabled)
    for plugin_id in plugin_ids:
        try:
            _plugin_name, marketplace_name = split_plugin_id(plugin_id)
        except ValueError:
            continue
        if marketplace_name == name:
            uninstall_plugin(plugin_id)

    removed = remove_marketplace_record(name)
    location = Path(record.install_location)
    try:
        resolved = location.resolve()
        cache_root = ensure_marketplace_cache_dir().resolve()
    except OSError:
        return removed
    if record.source_type in {"github", "git", "url"} and resolved.is_relative_to(
        cache_root
    ):
        if resolved.is_dir():
            shutil.rmtree(resolved, ignore_errors=True)
        elif resolved.is_file():
            resolved.unlink(missing_ok=True)
    return removed


def _require_installed_plugin(plugin_id: str) -> None:
    if plugin_id not in load_installed_plugins(strict=True):
        msg = f"Plugin {plugin_id!r} is not installed"
        raise MarketplaceError(msg)


@plugin_mutation_lock()
def set_installed_plugin_enabled(
    plugin_id: str,
    enabled: bool = True,
    *,
    scope: InstallScope = "user",
    project_root: Path | None = None,
) -> None:
    """Set the enabled state of an installed plugin at the given scope."""
    if scope != "project":
        try:
            _require_installed_plugin(plugin_id)
        except MarketplaceError:
            pass
    set_plugin_enabled_for_scope(
        plugin_id, enabled, scope=scope, project_root=project_root
    )
    if enabled:
        ensure_plugin_data_dir(plugin_id)


@plugin_mutation_lock()
def uninstall_plugin(
    plugin_id: str,
    *,
    scope: InstallScope | None = None,
    project_root: Path | None = None,
) -> None:
    """Uninstall a plugin (disable, clear records, delete orphaned cache)."""
    effective_scope = scope or ("project" if project_root else "user")
    set_plugin_enabled_for_scope(
        plugin_id, False, scope=effective_scope, project_root=project_root
    )
    uninstall_plugin_record(
        plugin_id, scope=scope, project_root=project_root
    )


def _resolve_marketplace_and_entry(
    plugin_id: str,
) -> tuple[PluginMarketplace, MarketplacePluginEntry]:
    try:
        plugin_name, marketplace_name = split_plugin_id(plugin_id)
    except ValueError as exc:
        raise MarketplaceError(str(exc)) from exc
    records = load_marketplace_records()
    record = records.get(marketplace_name)
    if record is None:
        msg = f"Marketplace {marketplace_name!r} is not configured"
        raise MarketplaceError(msg)
    marketplace = load_marketplace_location(Path(record.install_location))
    entry = next(
        (plugin for plugin in marketplace.plugins if plugin.name == plugin_name),
        None,
    )
    if entry is None:
        msg = f"Plugin {plugin_id!r} not found in marketplace {marketplace_name}"
        raise MarketplaceError(msg)
    return marketplace, entry


@plugin_mutation_lock()
def install_plugin(
    plugin_id: str,
    *,
    scope: InstallScope = "user",
    project_root: Path | None = None,
) -> PluginInstance:
    """Install a marketplace plugin into the versioned cache and enable it.

    Args:
        plugin_id: Plugin id in ``name@marketplace`` form.
        scope: Installation scope — ``"user"``, ``"project"``, or ``"local"``.
        project_root: Required when ``scope`` is ``"project"`` or ``"local"``.
    """
    load_installed_plugins(strict=True)
    marketplace, entry = _resolve_marketplace_and_entry(plugin_id)
    source_root = materialize_plugin_source(marketplace, entry)
    if source_root is None:
        msg = (
            f"Plugin {plugin_id} has unsupported source "
            f"{redact_urls_in_text(repr(entry.source))}; "
            "use a local path, GitHub repository, or Git repository source"
        )
        raise MarketplaceError(msg)

    try:
        manifest, _manifest_path, manifest_warnings = load_manifest(
            source_root, fallback_name=entry.name
        )
    except PluginManifestError as exc:
        msg = f"Cannot install {plugin_id}: {exc}"
        raise MarketplaceError(msg) from exc

    for warning in manifest_warnings:
        logger.debug("Plugin install warning for %s: %s", plugin_id, warning)

    version = manifest.version if manifest is not None else None
    cache_path = cache_and_register_plugin(
        plugin_id,
        source_root,
        version=version,
        scope=scope,
        project_root=project_root,
        validate=partial(
            _validate_plugin_copy,
            plugin_id=plugin_id,
            fallback_name=entry.name,
        ),
    )

    set_plugin_enabled_for_scope(
        plugin_id, True, scope=scope, project_root=project_root
    )
    ensure_plugin_data_dir(plugin_id)

    instance, warnings = _plugin_from_install_path(
        plugin_id=plugin_id,
        root=cache_path,
        marketplace_name=marketplace.name,
        fallback_name=entry.name,
    )
    if instance is None:
        detail = "; ".join(warnings)
        uninstall_plugin_record(
            plugin_id, scope=scope, project_root=project_root
        )
        msg = f"Installed {plugin_id} but failed to load from cache: {detail}"
        raise MarketplaceError(msg)
    return instance


def _validate_plugin_copy(
    root: Path,
    *,
    plugin_id: str,
    fallback_name: str,
) -> None:
    try:
        manifest, _manifest_path, warnings = load_manifest(
            root, fallback_name=fallback_name
        )
    except PluginManifestError as exc:
        msg = f"Cannot install {plugin_id}: {exc}"
        raise MarketplaceError(msg) from exc
    build_inventory(root, manifest, warnings)


def _plugin_from_install_path(
    *,
    plugin_id: str,
    root: Path,
    marketplace_name: str,
    fallback_name: str,
) -> tuple[PluginInstance | None, tuple[str, ...]]:
    warnings: list[str] = []
    try:
        manifest, _manifest_path, manifest_warnings = load_manifest(
            root, fallback_name=fallback_name
        )
    except PluginManifestError as exc:
        return None, (f"Skipping plugin {plugin_id}: {exc}",)
    warnings.extend(manifest_warnings)
    name = manifest.name if manifest and manifest.name else fallback_name
    inventory = build_inventory(root, manifest, tuple(warnings))
    try:
        instance = PluginInstance(
            plugin_id=plugin_id,
            name=name,
            marketplace=marketplace_name,
            version=manifest.version if manifest is not None else None,
            root=root,
            data_dir=plugin_data_dir(plugin_id),
            manifest=manifest,
            inventory=inventory,
        )
    except ValueError as exc:
        return None, (f"Skipping plugin {plugin_id}: {exc}",)
    return instance, inventory.warnings


def discover_plugins(
    project_root: Path | None = None,
    store: Any = None,
) -> PluginDiscoveryResult:
    """Discover enabled marketplace plugins from their install cache paths.

    Uses merged enablement across user + project + local scopes.
    """
    enabled = load_all_enabled_plugin_ids(project_root=project_root)
    plugins: list[PluginInstance] = []
    warnings: list[str] = []
    seen_plugin_ids: set[str] = set()

    # 1. Local filesystem discovery (e.g. project plugins directory)
    if project_root:
        proj_plugins = Path(project_root) / "plugins"
        if proj_plugins.is_dir():
            for p in sorted(proj_plugins.iterdir()):
                if p.is_dir() and not p.name.startswith("."):
                    p_id = f"{p.name}@project"
                    inst, p_warns = _plugin_from_install_path(
                        plugin_id=p_id,
                        root=p,
                        marketplace_name="project",
                        fallback_name=p.name,
                    )
                    warnings.extend(p_warns)
                    if inst:
                        plugins.append(inst)
                        seen_plugin_ids.add(p_id)

    enabled = load_all_enabled_plugin_ids(project_root=project_root)

    for plugin_id in sorted(enabled):
        if plugin_id in seen_plugin_ids:
            continue
        try:
            plugin_name, marketplace_name = split_plugin_id(plugin_id)
        except ValueError:
            warnings.append(f"Ignoring invalid plugin id {plugin_id!r}")
            continue
        entry = get_primary_install_entry(plugin_id)
        needs_install = entry is None
        if entry is not None:
            root = Path(entry.install_path)
            try:
                if not root.is_dir():
                    needs_install = True
            except (OSError, RuntimeError):
                needs_install = True

        if needs_install:
            try:
                scope: InstallScope = "user"
                if project_root:
                    if plugin_id in load_local_enabled_plugin_ids(project_root):
                        scope = "local"
                    elif plugin_id in load_project_enabled_plugin_ids(project_root):
                        scope = "project"
                instance = install_plugin(
                    plugin_id, scope=scope, project_root=project_root
                )
                if instance is not None:
                    plugins.append(instance)
                continue
            except Exception as exc:
                warnings.append(
                    f"Plugin {plugin_id} is enabled but auto-install failed: {exc}"
                )
                continue

        assert entry is not None
        root = Path(entry.install_path)
        try:
            plugin, plugin_warnings = _plugin_from_install_path(
                plugin_id=plugin_id,
                root=root,
                marketplace_name=marketplace_name,
                fallback_name=plugin_name,
            )
        except (OSError, RuntimeError) as exc:
            warnings.append(f"Skipping plugin {plugin_id}: {exc}")
            continue
        warnings.extend(plugin_warnings)
        if plugin is not None:
            plugins.append(plugin)

    return PluginDiscoveryResult(plugins=tuple(plugins), warnings=tuple(warnings))


def list_available_plugins(
    project_root: Path | None = None,
) -> tuple[tuple[str, str, bool], ...]:
    """List plugins from configured marketplaces."""
    records = load_marketplace_records(project_root=project_root)
    enabled = load_all_enabled_plugin_ids(project_root=project_root)
    rows: list[tuple[str, str, bool]] = []
    for name, record in sorted(records.items()):
        try:
            marketplace = load_marketplace_location(Path(record.install_location))
        except MarketplaceError as exc:
            rows.append((f"<marketplace:{name}>", str(exc), False))
            continue
        for plugin in marketplace.plugins:
            plugin_id = f"{plugin.name}@{marketplace.name}"
            rows.append((plugin_id, plugin.description or "", plugin_id in enabled))
    return tuple(rows)


def list_installed_plugin_ids() -> frozenset[str]:
    """Return plugin ids that have install records."""
    return frozenset(load_installed_plugins())
