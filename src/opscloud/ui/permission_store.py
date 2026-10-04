"""Permission store — central permission state management for OpsCloud.

Manages allow / ask / deny rules with both scope-based rules (e.g. ``shell:read``)
and tool-pattern rules (e.g. ``Shell(kubectl get *)``).  Rules are evaluated in
**Deny → Ask → Allow** order (first match wins), mirroring Claude Code's model.

Persistence is handled via ``~/.opscloud/config.toml`` under the ``[permissions]``
section.  Session-only state (recently denied actions) is *not* persisted.
"""

from __future__ import annotations

import fnmatch
from opscloud.utils.logger import get_logger
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

logger = get_logger(__name__)

# ── Scope definitions ────────────────────────────────────
VALID_SCOPES = frozenset({
    "shell:read", "shell:write",
    "file:read", "file:write",
    "infra:plan", "infra:apply",
})

_DEFAULT_ALLOW: list[str] = ["shell:read", "file:read"]
_DEFAULT_ASK: list[str] = ["shell:write", "file:write", "infra:plan"]
_DEFAULT_DENY: list[str] = ["infra:apply"]

# ── Permission Modes ─────────────────────────────────────
PERMISSION_MODES = {
    "default": "Standard mode — asks for approval on sensitive operations",
    "acceptEdits": "Auto-approves file reads/writes but prompts for shell/infra",
    "plan": "Allows reads and plan commands; blocks apply/write",
    "auto": "Auto-approves most operations with safety checks",
    "strict": "Everything requires approval (production recommended)",
}

# Mode presets mapping mode name -> (allow, ask, deny)
_MODE_PRESETS: dict[str, tuple[list[str], list[str], list[str]]] = {
    "default": (
        ["shell:read", "file:read"],
        ["shell:write", "file:write", "infra:plan"],
        ["infra:apply"],
    ),
    "acceptEdits": (
        ["shell:read", "file:read", "file:write"],
        ["shell:write", "infra:plan"],
        ["infra:apply"],
    ),
    "plan": (
        ["shell:read", "file:read", "infra:plan"],
        ["shell:write", "file:write"],
        ["infra:apply"],
    ),
    "auto": (
        ["shell:read", "file:read", "file:write", "shell:write", "infra:plan"],
        [],
        ["infra:apply"],
    ),
    "strict": (
        [],
        ["shell:read", "file:read", "shell:write", "file:write", "infra:plan", "infra:apply"],
        [],
    ),
}

# ── Rule pattern matching ────────────────────────────────
# Tool pattern format: ToolName(specifier) e.g. Shell(kubectl get *)
_TOOL_PATTERN_RE = re.compile(r"^(\w+)\((.+)\)$")


@dataclass
class PermissionRule:
    """A single permission rule."""

    pattern: str       # e.g., "shell:read", "Shell(kubectl get *)"
    source: str = "session"   # "default", "session", "config"
    created_at: float = field(default_factory=time.time)

    @property
    def is_scope(self) -> bool:
        """Whether this is a scope-based rule (e.g. shell:read)."""
        return self.pattern in VALID_SCOPES

    @property
    def is_tool_pattern(self) -> bool:
        """Whether this is a tool-pattern rule (e.g. Shell(kubectl get *))."""
        return bool(_TOOL_PATTERN_RE.match(self.pattern))

    @property
    def display_label(self) -> str:
        """Human-readable label."""
        if self.is_scope:
            parts = self.pattern.split(":")
            return f"{parts[0].title()} — {parts[1]}"
        return self.pattern


@dataclass
class DeniedAction:
    """A recently denied tool action."""

    tool_name: str
    call_id: str
    args: dict[str, Any] = field(default_factory=dict)
    denied_at: float = field(default_factory=time.time)
    comment: str = ""

    @property
    def display_label(self) -> str:
        """Short label for display."""
        if self.args:
            cmd = self.args.get("command", "")
            if cmd:
                short = cmd[:60] + "…" if len(str(cmd)) > 60 else str(cmd)
                return f"{self.tool_name}({short})"
        return self.tool_name


_MAX_RECENTLY_DENIED = 50


class PermissionStore:
    """Central permission state for the session.

    Rules are evaluated in **Deny → Ask → Allow** order.
    If no rule matches, the default is "ask" (require approval).
    """

    def __init__(self) -> None:
        self.allow: list[PermissionRule] = []
        self.ask: list[PermissionRule] = []
        self.deny: list[PermissionRule] = []
        self.recently_denied: deque[DeniedAction] = deque(maxlen=_MAX_RECENTLY_DENIED)
        self.mode: str = "default"
        self._apply_defaults()

    def _apply_defaults(self) -> None:
        """Set default rules."""
        self.allow = [PermissionRule(p, source="default") for p in _DEFAULT_ALLOW]
        self.ask = [PermissionRule(p, source="default") for p in _DEFAULT_ASK]
        self.deny = [PermissionRule(p, source="default") for p in _DEFAULT_DENY]

    def apply_mode(self, mode_name: str) -> bool:
        """Apply a permission mode preset, replacing all scope-based rules."""
        if mode_name not in _MODE_PRESETS:
            return False
        allow_p, ask_p, deny_p = _MODE_PRESETS[mode_name]

        # Keep tool-pattern rules, replace scope-based ones
        self.allow = [r for r in self.allow if r.is_tool_pattern]
        self.ask = [r for r in self.ask if r.is_tool_pattern]
        self.deny = [r for r in self.deny if r.is_tool_pattern]

        self.allow.extend(PermissionRule(p, source="mode") for p in allow_p)
        self.ask.extend(PermissionRule(p, source="mode") for p in ask_p)
        self.deny.extend(PermissionRule(p, source="mode") for p in deny_p)
        self.mode = mode_name
        return True

    def evaluate(self, scope: str) -> Literal["allow", "ask", "deny"]:
        """Evaluate rules in Deny → Ask → Allow order. Default is 'ask'."""
        for rule in self.deny:
            if self._matches(rule.pattern, scope):
                return "deny"
        for rule in self.ask:
            if self._matches(rule.pattern, scope):
                return "ask"
        for rule in self.allow:
            if self._matches(rule.pattern, scope):
                return "allow"
        return "ask"

    def evaluate_tool(self, tool_name: str, args: dict[str, Any] | None = None) -> Literal["allow", "ask", "deny"]:
        """Evaluate tool call against all rules including tool patterns."""
        # Build a tool specifier string for matching
        cmd = ""
        if args:
            cmd = str(args.get("command", ""))
        tool_spec = f"{tool_name}({cmd})" if cmd else tool_name

        for rule in self.deny:
            if self._matches(rule.pattern, tool_spec) or self._matches(rule.pattern, tool_name):
                return "deny"
        for rule in self.ask:
            if self._matches(rule.pattern, tool_spec) or self._matches(rule.pattern, tool_name):
                return "ask"
        for rule in self.allow:
            if self._matches(rule.pattern, tool_spec) or self._matches(rule.pattern, tool_name):
                return "allow"
        return "ask"

    @staticmethod
    def _matches(pattern: str, target: str) -> bool:
        """Check if a rule pattern matches a target scope or tool specifier."""
        # Exact scope match
        if pattern == target:
            return True
        # Tool pattern: Shell(kubectl get *) matches Shell(kubectl get pods)
        m = _TOOL_PATTERN_RE.match(pattern)
        if m:
            tool_name, specifier = m.group(1), m.group(2)
            t = _TOOL_PATTERN_RE.match(target)
            if t:
                target_tool, target_spec = t.group(1), t.group(2)
                if tool_name.lower() == target_tool.lower():
                    return fnmatch.fnmatch(target_spec, specifier)
            # Bare tool name match: pattern "Shell(npm *)" vs target "Shell"
            elif tool_name.lower() == target.lower():
                return False  # Pattern is more specific than target
        # Bare tool name match
        if pattern.lower() == target.lower():
            return True
        return False

    # ── Mutators ─────────────────────────────────────────

    def add_rule(self, category: Literal["allow", "ask", "deny"], pattern: str, source: str = "session") -> bool:
        """Add a rule to the given category, removing from other categories first."""
        # Remove from all categories first to prevent duplicates
        self._remove_pattern(pattern)
        rule = PermissionRule(pattern=pattern, source=source)
        target_list = self._get_list(category)
        target_list.append(rule)
        return True

    def remove_rule(self, category: Literal["allow", "ask", "deny"], pattern: str) -> bool:
        """Remove a rule from the given category."""
        target_list = self._get_list(category)
        before = len(target_list)
        target_list[:] = [r for r in target_list if r.pattern != pattern]
        return len(target_list) < before

    def _remove_pattern(self, pattern: str) -> None:
        """Remove a pattern from all categories."""
        for lst in (self.allow, self.ask, self.deny):
            lst[:] = [r for r in lst if r.pattern != pattern]

    def _get_list(self, category: Literal["allow", "ask", "deny"]) -> list[PermissionRule]:
        if category == "allow":
            return self.allow
        if category == "ask":
            return self.ask
        return self.deny

    def track_denied(self, tool_name: str, call_id: str, args: dict[str, Any] | None = None, comment: str = "") -> None:
        """Track a denied tool action for the Recently Denied tab."""
        self.recently_denied.appendleft(
            DeniedAction(
                tool_name=tool_name,
                call_id=call_id,
                args=args or {},
                comment=comment,
            )
        )

    def reset(self) -> None:
        """Reset all rules to defaults and clear recently denied."""
        self.mode = "default"
        self.recently_denied.clear()
        self._apply_defaults()

    # ── Legacy compatibility ─────────────────────────────

    def get_scope_status(self, scope: str) -> bool:
        """Legacy: returns True if scope is allowed (no approval needed)."""
        return self.evaluate(scope) == "allow"

    def to_legacy_dict(self) -> dict[str, bool]:
        """Legacy: return flat dict compatible with old _permission_scopes."""
        return {scope: self.get_scope_status(scope) for scope in sorted(VALID_SCOPES)}

    # ── Persistence ──────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dictionary for config.toml persistence."""
        return {
            "mode": self.mode,
            "allow": [r.pattern for r in self.allow if r.source != "default"],
            "ask": [r.pattern for r in self.ask if r.source != "default"],
            "deny": [r.pattern for r in self.deny if r.source != "default"],
        }

    def load_from_dict(self, data: dict[str, Any]) -> None:
        """Load persisted rules from a config dictionary.

        Merges config rules on top of default rules.
        """
        self._apply_defaults()
        self.mode = data.get("mode", "default")

        # If a non-default mode is set, apply it first
        if self.mode != "default" and self.mode in _MODE_PRESETS:
            self.apply_mode(self.mode)

        # Then overlay any custom rules from config
        for pattern in data.get("allow", []):
            if isinstance(pattern, str) and pattern:
                self.add_rule("allow", pattern, source="config")
        for pattern in data.get("ask", []):
            if isinstance(pattern, str) and pattern:
                self.add_rule("ask", pattern, source="config")
        for pattern in data.get("deny", []):
            if isinstance(pattern, str) and pattern:
                self.add_rule("deny", pattern, source="config")


def load_permission_store() -> PermissionStore:
    """Load a PermissionStore from ``~/.opscloud/config.toml``."""
    store = PermissionStore()
    config_path = Path(os.path.expanduser("~/.opscloud/config.toml"))
    if not config_path.exists():
        return store
    try:
        import tomllib
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
        perms = data.get("permissions", {})
        if isinstance(perms, dict):
            store.load_from_dict(perms)
    except Exception:
        logger.warning("Failed to load permissions from config.toml", exc_info=True)
    return store


def save_permission_store(store: PermissionStore) -> bool:
    """Persist a PermissionStore to ``~/.opscloud/config.toml``.

    Uses theme.py's single config writer to avoid data loss from
    competing serialisers.  Reads the full config, updates the
    ``[permissions]`` section, and writes everything back.
    """
    try:
        from opscloud.ui.theme import _read_config_toml_data, _write_config_toml_data

        existing = _read_config_toml_data()
        existing["permissions"] = store.to_dict()
        return _write_config_toml_data(existing)
    except Exception:
        logger.exception("Failed to save permissions to config.toml")
        return False


__all__ = [
    "DeniedAction",
    "PERMISSION_MODES",
    "PermissionRule",
    "PermissionStore",
    "VALID_SCOPES",
    "load_permission_store",
    "save_permission_store",
]

