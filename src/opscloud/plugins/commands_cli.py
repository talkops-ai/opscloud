"""CLI helpers for plugin management in OpsCloud."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Callable

from opscloud.plugins.discovery import (
    add_marketplace_source,
    install_plugin,
    list_available_plugins,
    remove_marketplace,
    set_installed_plugin_enabled,
    uninstall_plugin,
)
from opscloud.plugins.marketplace import (
    MarketplaceError,
    redact_marketplace_source,
    redact_urls_in_text,
)
from opscloud.plugins.models import InstallScope
from opscloud.plugins.store import load_marketplace_records


def _resolve_project_root(scope: str | None) -> Path | None:
    """Resolve the project root for project/local scoped commands.

    Returns ``None`` when ``scope`` is ``"user"`` or the project root
    cannot be determined.
    """
    if scope in {None, "user"}:
        return None
    from opscloud.config.paths import find_project_root

    root = find_project_root()
    if root is None and scope in {"project", "local"}:
        # Fall back to cwd when no marker is found
        root = Path.cwd()
    return root


def setup_plugin_parser(
    subparsers: Any,  # noqa: ANN401
    *,
    make_help_action: Callable[[Callable[[], None]], type[argparse.Action]] | None = None,
    add_output_args: Callable[[argparse.ArgumentParser], None] | None = None,
) -> argparse.ArgumentParser:
    """Set up the `plugin` CLI parser.

    Args:
        subparsers: Parent argparse subparsers object.
        make_help_action: Optional factory for parser-specific help actions.
        add_output_args: Optional callback that adds output-format flags.

    Returns:
        Plugin command parser.
    """

    def _default_help() -> None:
        print("Usage: opscloud plugin {list,install,uninstall,enable,disable,marketplace}")

    parent = argparse.ArgumentParser(add_help=False)
    if make_help_action is not None:
        parent.add_argument("-h", "--help", action=make_help_action(_default_help))
    else:
        parent.add_argument("-h", "--help", action="help")

    parser = subparsers.add_parser(
        "plugin",
        aliases=["plugins"],
        help="Manage plugins",
        add_help=False,
        parents=[parent],
    )
    if add_output_args is not None:
        add_output_args(parser)
    plugin_sub = parser.add_subparsers(dest="plugin_command")

    list_parser = plugin_sub.add_parser("list", aliases=["ls"], help="List plugins")
    if add_output_args is not None:
        add_output_args(list_parser)

    install_parser = plugin_sub.add_parser("install", help="Install a plugin")
    install_parser.add_argument("plugin_id")
    install_parser.add_argument(
        "--scope",
        choices=["user", "project", "local"],
        default="user",
        help="Installation scope: user (global), project (shared), or local (gitignored)",
    )
    uninstall_parser = plugin_sub.add_parser("uninstall", help="Uninstall a plugin")
    uninstall_parser.add_argument("plugin_id")
    uninstall_parser.add_argument(
        "--scope",
        choices=["user", "project", "local"],
        default=None,
        help="Scope to uninstall from (omit to remove all scopes)",
    )

    enable_parser = plugin_sub.add_parser("enable", help="Enable a plugin")
    enable_parser.add_argument("plugin_id")
    enable_parser.add_argument(
        "--scope",
        choices=["user", "project", "local"],
        default="user",
        help="Scope to enable in",
    )
    disable_parser = plugin_sub.add_parser("disable", help="Disable a plugin")
    disable_parser.add_argument("plugin_id")
    disable_parser.add_argument(
        "--scope",
        choices=["user", "project", "local"],
        default="user",
        help="Scope to disable in",
    )

    marketplace_parser = plugin_sub.add_parser(
        "marketplace", help="Manage plugin marketplaces"
    )
    marketplace_sub = marketplace_parser.add_subparsers(dest="marketplace_command")
    marketplace_list = marketplace_sub.add_parser(
        "list", aliases=["ls"], help="List marketplaces"
    )
    if add_output_args is not None:
        add_output_args(marketplace_list)
    marketplace_add = marketplace_sub.add_parser("add", help="Add a marketplace")
    marketplace_add.add_argument("source")
    marketplace_remove = marketplace_sub.add_parser(
        "remove", help="Remove a marketplace and uninstall its plugins"
    )
    marketplace_remove.add_argument("name")
    return parser


def _plugin_list_rows(project_root: Path | None = None) -> list[dict[str, object]]:
    return [
        {"id": plugin_id, "description": description, "enabled": enabled}
        for plugin_id, description, enabled in list_available_plugins(project_root=project_root)
    ]


def execute_plugin_command(args: argparse.Namespace) -> str | None:
    """Execute a plugin management command.

    Args:
        args: Parsed argparse namespace.

    Returns:
        Text output for slash-command callers, or `None` when output was written.

    Raises:
        SystemExit: With status 1 when a mutating command fails.
    """
    output_format = getattr(args, "output_format", "text")
    command = getattr(args, "plugin_command", None)
    if command is None:
        text = "Usage: opscloud plugin {list,install,uninstall,enable,disable,marketplace}"
        print(text)
        return text
    if command in {"list", "ls"}:
        project_root = _resolve_project_root("project")
        rows = _plugin_list_rows(project_root=project_root)
        if output_format == "json":
            import json
            text = json.dumps({"plugins": rows}, indent=2)
            print(text)
            return None
        if not rows:
            text = "No plugin marketplaces configured."
        else:
            lines = []
            for row in rows:
                status = "enabled" if row["enabled"] else "disabled"
                lines.append(f"{status} {row['id']} {row['description']}".rstrip())
            text = "\n".join(lines)
        print(text)
        return text
    if command == "install":
        raw_scope = getattr(args, "scope", "user") or "user"
        scope = cast(InstallScope, raw_scope)
        project_root = _resolve_project_root(scope)
        try:
            instance = install_plugin(
                args.plugin_id, scope=scope, project_root=project_root,
            )
        except (MarketplaceError, FileNotFoundError, OSError, ValueError) as exc:
            text = f"Failed to install {args.plugin_id}: {exc}"
            print(text)
            raise SystemExit(1) from exc
        details = ""
        if instance.version is not None:
            details = f" (version: {instance.version})"
        scope_label = {"user": "for you", "project": "for all collaborators", "local": "for you in this repo"}
        text = (
            f"Installed plugin {instance.plugin_id}{details} "
            f"({scope_label[scope]}). Run /reload to activate."
        )
        print(text)
        return text
    if command == "uninstall":
        raw_scope = getattr(args, "scope", None)
        scope = cast(InstallScope, raw_scope) if raw_scope else None
        project_root = _resolve_project_root(scope) if scope else None
        uninstall_plugin(args.plugin_id, scope=scope, project_root=project_root)
        text = f"Uninstalled plugin {args.plugin_id}."
        print(text)
        return text
    if command in {"enable", "disable"}:
        enabled = command == "enable"
        raw_scope = getattr(args, "scope", "user") or "user"
        scope = cast(InstallScope, raw_scope)
        project_root = _resolve_project_root(scope)
        try:
            set_installed_plugin_enabled(
                args.plugin_id, enabled=enabled, scope=scope, project_root=project_root,
            )
        except (MarketplaceError, OSError, ValueError) as exc:
            text = f"Failed to {command} {args.plugin_id}: {exc}"
            print(text)
            raise SystemExit(1) from exc
        text = f"{command.title()}d plugin {args.plugin_id}."
        print(text)
        return text
    if command == "marketplace":
        marketplace_command = getattr(args, "marketplace_command", None)
        if marketplace_command in {"list", "ls"}:
            records = load_marketplace_records()
            rows = [
                {
                    "name": record.name,
                    "source_type": record.source_type,
                    "source": redact_marketplace_source(record.source),
                    "install_location": (
                        record.install_location
                        if record.source_type in {"directory", "file"}
                        else "<managed cache>"
                    ),
                }
                for record in records.values()
            ]
            if output_format == "json":
                import json
                text = json.dumps({"marketplaces": rows}, indent=2)
                print(text)
                return None
            text = (
                "No plugin marketplaces configured."
                if not rows
                else "\n".join(f"{row['name']} {row['source']}" for row in rows)
            )
            print(text)
            return text
        if marketplace_command == "add":
            try:
                marketplace = add_marketplace_source(args.source)
            except (MarketplaceError, FileNotFoundError, OSError, ValueError) as exc:
                source = redact_marketplace_source(args.source)
                text = (
                    f"Failed to add marketplace {source}: "
                    f"{redact_urls_in_text(str(exc))}"
                )
                print(text)
                raise SystemExit(1) from exc
            text = (
                f"Added marketplace {marketplace.name} "
                f"({len(marketplace.plugins)} plugin(s))."
            )
            print(text)
            return text
        if marketplace_command == "remove":
            removed = remove_marketplace(args.name)
            text = (
                f"Removed marketplace {args.name} and its installed plugins."
                if removed
                else f"Marketplace {args.name} is not configured."
            )
            print(text)
            return text
    text = "Usage: opscloud plugin {list,install,uninstall,enable,disable,marketplace}"
    print(text)
    return text
