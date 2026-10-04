"""Permission scope management command handler for OpsCode.

Enhanced to open an interactive modal screen (PermissionsManagerScreen)
when called without arguments, while retaining CLI sub-commands for
scripting: ``grant``, ``revoke``, ``reset``, ``mode``.
"""

from __future__ import annotations

from opscloud.utils.logger import get_logger

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel
from opscloud.ui.permission_store import (
    PERMISSION_MODES,
    PermissionStore,
    VALID_SCOPES,
    save_permission_store,
)

logger = get_logger(__name__)


def _get_store(ctx: CommandContext) -> PermissionStore | None:
    """Retrieve the PermissionStore from the app, if available."""
    if ctx.app and hasattr(ctx.app, "_permission_store"):
        return ctx.app._permission_store
    return None


class PermissionsHandler(BaseCommandHandler):
    """Handler for /permissions — scoped permission management with interactive TUI."""

    @property
    def name(self) -> str:
        return "/permissions"

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.CORE

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.READ_ONLY

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.IMMEDIATE_UI

    async def execute(self, ctx: CommandContext) -> CommandResult:
        parts = ctx.args.strip().split(maxsplit=1)
        sub = parts[0].lower() if parts and parts[0] else "show"

        if sub == "show":
            return self._open_modal(ctx)
        if sub == "grant" and len(parts) > 1:
            return self._grant(ctx, parts[1].strip())
        if sub == "revoke" and len(parts) > 1:
            return self._revoke(ctx, parts[1].strip())
        if sub == "reset":
            return self._reset(ctx)
        if sub == "mode" and len(parts) > 1:
            return self._set_mode(ctx, parts[1].strip())
        if sub == "mode":
            return self._show_mode(ctx)

        return CommandResult(
            success=False,
            message=(
                "Usage: /permissions [show|grant <scope>|revoke <scope>|reset|mode [name]]\n"
                f"Valid scopes: {', '.join(sorted(VALID_SCOPES))}\n"
                f"Valid modes: {', '.join(PERMISSION_MODES)}"
            ),
        )

    # ── Interactive modal ────────────────────────────────

    def _open_modal(self, ctx: CommandContext) -> CommandResult:
        """Open the interactive permission management screen."""
        if ctx.app is None:
            return self._show_text_fallback(ctx)

        store = _get_store(ctx)
        if store is None:
            return CommandResult(success=False, message="Permission store not available (no app context).")

        from opscloud.ui.widgets.permissions_manager import PermissionsManagerScreen

        screen = PermissionsManagerScreen(store=store)

        def _on_close(_result: None) -> None:
            # Refocus chat input after permissions manager closes
            if hasattr(ctx.app, "_chat_input") and ctx.app._chat_input:
                ctx.app._chat_input.focus_input()

        ctx.app.push_screen(screen, _on_close)
        return CommandResult(success=True, message="", mount_as_app_message=False)

    def _show_text_fallback(self, ctx: CommandContext) -> CommandResult:
        """Text-only fallback when no app context is available."""
        store = _get_store(ctx)
        if store is None:
            return CommandResult(success=False, message="Permission store not available.")

        lines = [f"🔐 **Permission Scopes** (mode: {store.mode})\n"]
        for scope in sorted(VALID_SCOPES):
            status = store.evaluate(scope)
            icons = {"allow": "✅", "ask": "🟡", "deny": "🔒"}
            labels = {"allow": "granted", "ask": "requires approval", "deny": "denied"}
            lines.append(f"  {icons[status]} `{scope}`: {labels[status]}")

        # Tool-pattern rules
        tool_rules = [r for r in (store.allow + store.ask + store.deny) if r.is_tool_pattern]
        if tool_rules:
            lines.append("\n**Tool-Pattern Rules:**")
            for rule in tool_rules:
                cat = "allow" if rule in store.allow else "ask" if rule in store.ask else "deny"
                icons = {"allow": "✅", "ask": "🟡", "deny": "🔒"}
                lines.append(f"  {icons[cat]} `{rule.pattern}` [{cat}]")

        lines.append("\n_Use `/permissions` in TUI for interactive management._")
        return CommandResult(success=True, message="\n".join(lines))

    # ── CLI sub-commands ─────────────────────────────────

    def _grant(self, ctx: CommandContext, scope: str) -> CommandResult:
        """Grant (allow) a scope without requiring approval."""
        store = _get_store(ctx)
        if store is None:
            return CommandResult(success=False, message="Permission store not available (no app context).")

        # Accept both scope-based and tool-pattern rules
        store.add_rule("allow", scope, source="session")
        self._persist(store)
        return CommandResult(success=True, message=f"✅ Granted (Allow): `{scope}`")

    def _revoke(self, ctx: CommandContext, scope: str) -> CommandResult:
        """Revoke a scope — move it to 'ask' (require approval)."""
        store = _get_store(ctx)
        if store is None:
            return CommandResult(success=False, message="Permission store not available (no app context).")

        store.add_rule("ask", scope, source="session")
        self._persist(store)
        return CommandResult(success=True, message=f"🟡 Revoked: `{scope}` — now requires approval")

    def _reset(self, ctx: CommandContext) -> CommandResult:
        """Reset all permissions to defaults."""
        store = _get_store(ctx)
        if store is None:
            return CommandResult(success=False, message="Permission store not available (no app context).")

        store.reset()
        self._persist(store)
        return CommandResult(success=True, message="🔄 Permissions reset to defaults.")

    def _set_mode(self, ctx: CommandContext, mode_name: str) -> CommandResult:
        """Set a permission mode preset."""
        store = _get_store(ctx)
        if store is None:
            return CommandResult(success=False, message="Permission store not available (no app context).")

        if mode_name not in PERMISSION_MODES:
            return CommandResult(
                success=False,
                message=f"Unknown mode: `{mode_name}`. Valid: {', '.join(PERMISSION_MODES)}",
            )

        store.apply_mode(mode_name)
        self._persist(store)
        desc = PERMISSION_MODES[mode_name]
        return CommandResult(success=True, message=f"🔧 Permission mode set to `{mode_name}` — {desc}")

    def _show_mode(self, ctx: CommandContext) -> CommandResult:
        """Show current permission mode and available modes."""
        store = _get_store(ctx)
        if store is None:
            return CommandResult(success=False, message="Permission store not available (no app context).")

        lines = [f"🔧 **Current mode:** `{store.mode}`\n", "**Available modes:**"]
        for mode, desc in PERMISSION_MODES.items():
            marker = " ← current" if mode == store.mode else ""
            lines.append(f"  `{mode}`: {desc}{marker}")
        lines.append("\n_Usage: `/permissions mode <name>`_")
        return CommandResult(success=True, message="\n".join(lines))

    @staticmethod
    def _persist(store: PermissionStore) -> None:
        """Save permission state to disk."""
        try:
            save_permission_store(store)
        except Exception:
            logger.warning("Failed to persist permissions", exc_info=True)


__all__ = ["PermissionsHandler"]
