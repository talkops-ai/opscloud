"""Structured CLI settings and workspace trust manager for OpsCloud.

Provides persistent configuration and workspace trust management:
- Path resolution: ``~/.opscloud/settings.json`` (``resolve_cli_settings_path``)
  and path canonicalization without trailing slashes (``resolve_path_no_slash``).
- Structured ``CliSetting`` schema with camelCase JSON keys, strict field
  validation (``parse_settings_fields``, ``validate_cli_settings``), and
  preservation of ``unknown_fields`` across round-trips.
- Sparse JSON marshaling (``CliSetting.marshal_sparse``): omits default values
  (where ``editor`` defaults to ``"terminal"``), formats with 2-space indent
  and trailing newline, and normalizes on-disk settings on read.
- Three-way merge on write (``merge_cli_settings``): re-reads disk before
  writing, refuses to clobber malformed disk JSON, and merges concurrent disk
  edits with in-memory modifications relative to the base snapshot.
- Atomic file persistence (``atomic_write_settings``): writes to
  ``<path>.<uuid4>.tmp`` with mode ``0600`` (parent dir ``0755``) and
  atomically replaces ``<path>``.
- Corruption quarantine (``quarantine_cli_settings``): backs up malformed
  settings to ``<path>.corrupted.<unix_ms>`` and resets to defaults.
- Startup workspace trust evaluation and persistence (``is_trusted_workspace``,
  ``is_startup_workspace_trusted``, ``init_workspace_dirs``,
  ``workspace_trusted``, ``trust_workspace``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import contextlib
import copy
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import threading
import time
from typing import Any
import uuid

from opscloud.config import paths
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_EDITOR = "terminal"

_VALID_ALT_SCREEN_MODES = frozenset({"", "auto", "on", "off", "true", "false"})
_VALID_TOOL_PERMISSIONS = frozenset({
    "",
    "auto",
    "ask",
    "manual",
    "smart",
    "deny",
    "default",
    "acceptEdits",
    "plan",
    "strict",
})
_VALID_EDITOR_MODES = frozenset({"", "default", "vim", "emacs", "normal"})
_VALID_VERBOSITY_LEVELS = frozenset({"", "quiet", "normal", "verbose", "debug"})


class SettingsError(ValueError):
    """Base error for CLI settings parsing or validation failures."""


class SettingsMalformedError(SettingsError):
    """Raised when ``settings.json`` contains malformed JSON syntax."""


class SettingsValidationError(SettingsError):
    """Raised when ``settings.json`` contains invalid field types or values."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = list(errors)
        joined = "; ".join(self.errors)
        super().__init__(f"invalid settings: {joined}")


def resolve_cli_settings_path() -> Path:
    """Resolve the active CLI ``settings.json`` path.

    Defaults to ``~/.opscloud/settings.json`` while respecting test overrides
    applied to ``paths.USER_SETTINGS_PATH`` or ``paths.DATA_DIR``.
    """
    default_user_settings = Path.home() / ".opscloud" / "settings.json"
    if paths.USER_SETTINGS_PATH != default_user_settings:
        return paths.USER_SETTINGS_PATH
    return paths.DATA_DIR / "settings.json"


def resolve_path_no_slash(raw_path: str | Path) -> str:
    """Normalize a filesystem path and strip trailing slashes.

    - Expands leading ``~`` or ``~/`` using the user's home directory.
    - Applies ``os.path.normpath`` to collapse redundant separators and
      up-level references while stripping trailing slashes (preserving ``"/"`` for root).
    """
    s = str(raw_path).strip() if raw_path is not None else ""
    if not s:
        return ""
    if s == "~":
        try:
            return os.path.normpath(str(Path.home()))
        except Exception:
            return "~"
    if s.startswith("~/") or s.startswith("~\\"):
        try:
            home = str(Path.home())
            s = os.path.join(home, s[2:])
        except Exception:
            pass
    return os.path.normpath(s)


def is_valid_unified_rule(rule: str) -> bool:
    """Validate a unified permission rule string.

    Checks that the rule is a non-empty string with balanced parentheses if a
    specifier is present (e.g. ``Shell(kubectl get *)`` or ``shell:read``).
    """
    if not isinstance(rule, str):
        return False
    stripped = rule.strip()
    if not stripped:
        return False
    if "(" in stripped or ")" in stripped:
        if not stripped.endswith(")") or stripped.count("(") != 1 or stripped.count(")") != 1:
            return False
        tool_part, _, spec_part = stripped[:-1].partition("(")
        if not tool_part.strip() or not spec_part.strip():
            return False
    return True


@dataclass
class PermissionsSetting:
    """Global tool/scope permissions block."""

    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    ask: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.allow or self.deny or self.ask)

    def to_sparse_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.allow:
            out["allow"] = list(self.allow)
        if self.deny:
            out["deny"] = list(self.deny)
        if self.ask:
            out["ask"] = list(self.ask)
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | dict[str, Any]) -> tuple[PermissionsSetting, list[str]]:
        errors: list[str] = []
        allow: list[str] = []
        deny: list[str] = []
        ask: list[str] = []

        for key, target in (("allow", allow), ("deny", deny), ("ask", ask)):
            if key not in data or data[key] is None:
                continue
            raw_list = data[key]
            if not isinstance(raw_list, list):
                errors.append(f"permissions.{key}: expected array of strings")
                continue
            for idx, item in enumerate(raw_list):
                if not isinstance(item, str) or not is_valid_unified_rule(item):
                    errors.append(f"permissions.{key}[{idx}]: invalid permission rule {item!r}")
                else:
                    target.append(item.strip())

        return cls(allow=allow, deny=deny, ask=ask), errors


@dataclass
class CliSetting:
    """Structured CLI settings schema for OpsCloud.

    Preserves unknown JSON fields in ``unknown_fields`` so external or future
    keys in ``~/.opscloud/settings.json`` are never lost on save.
    """

    color_scheme: str = ""
    alt_screen_mode: str = ""
    trusted_workspaces: list[str] = field(default_factory=list)
    tool_permission: str = ""
    permissions: PermissionsSetting | None = None
    history_size: int = 0
    model: str = ""
    custom_models_config: dict[str, Any] | None = None
    context_window: int = 0
    notifications: bool | None = None
    editor: str = DEFAULT_EDITOR
    editor_mode: str = ""
    vim_insert_first: bool = False
    clear_scrollback_on_resize: bool = False
    allow_non_workspace_access: bool = False
    use_terminal_cursor: bool = False
    verbosity: str = ""
    artifact_review_policy: str = ""
    running_light_speed: bool = False
    status_line: dict[str, Any] | str | None = None
    title: dict[str, Any] | str | None = None
    show_tips: bool | None = None
    enable_terminal_sandbox: bool = False
    sandbox_proxy: dict[str, Any] | None = None
    gcp: dict[str, Any] | None = None
    show_feedback_survey: bool = False
    use_g1_credits: bool = False
    hide_active_items: bool = False
    agent_mode: str = ""
    copy_on_select: bool | None = None
    disable_slash_commands: bool = False
    model_provider: str = ""
    picker_grouping: str = ""
    queued_messages: str = ""
    auto_compact: str = ""

    # OpsCloud structured extensions
    enabled_plugins: dict[str, bool] = field(default_factory=dict)
    reasoning_effort: str = ""
    aws_profile: str = ""
    aws_region: str = ""
    agent_pool: dict[str, str] = field(default_factory=dict)

    # Preserved unrecognized keys
    unknown_fields: dict[str, Any] = field(default_factory=dict, repr=False)

    def is_trusted_workspace(self, workspace_dir: str | Path) -> bool:
        """Check whether ``workspace_dir`` is in ``trustedWorkspaces``.

        Checks both exact string equality and ``resolve_path_no_slash`` normalized equality.
        """
        raw = str(workspace_dir).strip() if workspace_dir is not None else ""
        if not raw:
            return False
        cleaned = resolve_path_no_slash(raw)
        for entry in self.trusted_workspaces:
            if not entry:
                continue
            if entry == raw or entry == cleaned:
                return True
            if resolve_path_no_slash(entry) == cleaned:
                return True
        return False

    def to_sparse_dict(self) -> dict[str, Any]:
        """Return a sparse dictionary containing only non-default fields + unknown_fields."""
        defaults = default_cli_settings()
        out: dict[str, Any] = {}

        if self.color_scheme != defaults.color_scheme:
            out["colorScheme"] = self.color_scheme
        if self.alt_screen_mode != defaults.alt_screen_mode:
            out["altScreenMode"] = self.alt_screen_mode
        if self.trusted_workspaces:
            out["trustedWorkspaces"] = list(self.trusted_workspaces)
        if self.tool_permission != defaults.tool_permission:
            out["toolPermission"] = self.tool_permission
        if self.permissions is not None and not self.permissions.is_empty():
            out["permissions"] = self.permissions.to_sparse_dict()
        if self.history_size != defaults.history_size:
            out["historySize"] = self.history_size
        if self.model != defaults.model:
            out["model"] = self.model
        if self.custom_models_config:
            out["customModelsConfig"] = copy.deepcopy(self.custom_models_config)
        if self.context_window != defaults.context_window:
            out["contextWindow"] = self.context_window
        if self.notifications is not None:
            out["notifications"] = self.notifications
        if self.editor != defaults.editor:
            out["editor"] = self.editor
        if self.editor_mode != defaults.editor_mode:
            out["editorMode"] = self.editor_mode
        if self.vim_insert_first != defaults.vim_insert_first:
            out["vimInsertFirst"] = self.vim_insert_first
        if self.clear_scrollback_on_resize != defaults.clear_scrollback_on_resize:
            out["clearScrollbackOnResize"] = self.clear_scrollback_on_resize
        if self.allow_non_workspace_access != defaults.allow_non_workspace_access:
            out["allowNonWorkspaceAccess"] = self.allow_non_workspace_access
        if self.use_terminal_cursor != defaults.use_terminal_cursor:
            out["useTerminalCursor"] = self.use_terminal_cursor
        if self.verbosity != defaults.verbosity:
            out["verbosity"] = self.verbosity
        if self.artifact_review_policy != defaults.artifact_review_policy:
            out["artifactReviewPolicy"] = self.artifact_review_policy
        if self.running_light_speed != defaults.running_light_speed:
            out["runningLightSpeed"] = self.running_light_speed
        if self.status_line is not None:
            out["statusLine"] = copy.deepcopy(self.status_line)
        if self.title is not None:
            out["title"] = copy.deepcopy(self.title)
        if self.show_tips is not None:
            out["showTips"] = self.show_tips
        if self.enable_terminal_sandbox != defaults.enable_terminal_sandbox:
            out["enableTerminalSandbox"] = self.enable_terminal_sandbox
        if self.sandbox_proxy is not None:
            out["sandboxProxy"] = copy.deepcopy(self.sandbox_proxy)
        if self.gcp is not None:
            out["gcp"] = copy.deepcopy(self.gcp)
        if self.show_feedback_survey != defaults.show_feedback_survey:
            out["showFeedbackSurvey"] = self.show_feedback_survey
        if self.use_g1_credits != defaults.use_g1_credits:
            out["useG1Credits"] = self.use_g1_credits
        if self.hide_active_items != defaults.hide_active_items:
            out["hideActiveItems"] = self.hide_active_items
        if self.agent_mode != defaults.agent_mode:
            out["agentMode"] = self.agent_mode
        if self.copy_on_select is not None:
            out["copyOnSelect"] = self.copy_on_select
        if self.disable_slash_commands != defaults.disable_slash_commands:
            out["disableSlashCommands"] = self.disable_slash_commands
        if self.model_provider != defaults.model_provider:
            out["modelProvider"] = self.model_provider
        if self.picker_grouping != defaults.picker_grouping:
            out["pickerGrouping"] = self.picker_grouping
        if self.queued_messages != defaults.queued_messages:
            out["queuedMessages"] = self.queued_messages
        if self.auto_compact != defaults.auto_compact:
            out["autoCompact"] = self.auto_compact
        if self.enabled_plugins:
            out["enabledPlugins"] = dict(self.enabled_plugins)
        if self.reasoning_effort != defaults.reasoning_effort:
            out["reasoningEffort"] = self.reasoning_effort
        if self.aws_profile != defaults.aws_profile:
            out["awsProfile"] = self.aws_profile
        if self.aws_region != defaults.aws_region:
            out["awsRegion"] = self.aws_region
        if self.agent_pool:
            out["agentPool"] = dict(self.agent_pool)

        for k, v in self.unknown_fields.items():
            if k not in out:
                out[k] = copy.deepcopy(v)

        return out

    def marshal_sparse(self) -> bytes:
        """Serialize to canonical 2-space indented sparse JSON bytes with trailing newline."""
        sparse = self.to_sparse_dict()
        if not sparse:
            return b"{}\n"
        return (json.dumps(sparse, indent=2) + "\n").encode("utf-8")


def default_cli_settings() -> CliSetting:
    """Return a fresh ``CliSetting`` populated with default values."""
    return CliSetting(editor=DEFAULT_EDITOR)


# Mapping of camelCase JSON keys -> (dataclass attribute, expected type description)
_KNOWN_JSON_KEYS = frozenset({
    "colorScheme",
    "altScreenMode",
    "trustedWorkspaces",
    "toolPermission",
    "permissions",
    "historySize",
    "model",
    "customModelsConfig",
    "contextWindow",
    "notifications",
    "editor",
    "editorMode",
    "vimInsertFirst",
    "clearScrollbackOnResize",
    "allowNonWorkspaceAccess",
    "useTerminalCursor",
    "verbosity",
    "artifactReviewPolicy",
    "runningLightSpeed",
    "statusLine",
    "title",
    "showTips",
    "enableTerminalSandbox",
    "sandboxProxy",
    "gcp",
    "showFeedbackSurvey",
    "useG1Credits",
    "hideActiveItems",
    "agentMode",
    "copyOnSelect",
    "disableSlashCommands",
    "modelProvider",
    "pickerGrouping",
    "queuedMessages",
    "autoCompact",
    "enabledPlugins",
    "reasoningEffort",
    "awsProfile",
    "awsRegion",
    "agentPool",
})


def parse_settings_fields(raw_map: dict[str, Any]) -> tuple[CliSetting, list[str]]:
    """Parse and validate a raw JSON object into ``CliSetting`` and a list of field errors."""
    setting = default_cli_settings()
    errors: list[str] = []

    if not isinstance(raw_map, dict):
        return setting, ["settings root must be a JSON object"]

    def _expect_str(key: str, allowed: frozenset[str] | None = None) -> str | None:
        if key not in raw_map or raw_map[key] is None:
            return None
        val = raw_map[key]
        if not isinstance(val, str):
            errors.append(f"{key}: expected string, got {type(val).__name__}")
            return None
        if allowed is not None and val not in allowed:
            errors.append(f"{key}: invalid value {val!r}")
            return None
        return val

    def _expect_bool(key: str) -> bool | None:
        if key not in raw_map or raw_map[key] is None:
            return None
        val = raw_map[key]
        if not isinstance(val, bool):
            errors.append(f"{key}: expected boolean, got {type(val).__name__}")
            return None
        return val

    def _expect_nonneg_int(key: str) -> int | None:
        if key not in raw_map or raw_map[key] is None:
            return None
        val = raw_map[key]
        if isinstance(val, bool) or not isinstance(val, int) or val < 0:
            errors.append(f"{key}: expected non-negative integer, got {val!r}")
            return None
        return val

    if (v := _expect_str("colorScheme")) is not None:
        setting.color_scheme = v
    if (v := _expect_str("altScreenMode", _VALID_ALT_SCREEN_MODES)) is not None:
        setting.alt_screen_mode = v

    if "trustedWorkspaces" in raw_map and raw_map["trustedWorkspaces"] is not None:
        tw = raw_map["trustedWorkspaces"]
        if not isinstance(tw, list):
            errors.append(f"trustedWorkspaces: expected array of strings, got {type(tw).__name__}")
        else:
            workspaces: list[str] = []
            for idx, item in enumerate(tw):
                if not isinstance(item, str) or not item.strip():
                    errors.append(f"trustedWorkspaces[{idx}]: expected non-empty string")
                else:
                    workspaces.append(resolve_path_no_slash(item))
            setting.trusted_workspaces = workspaces

    if (v := _expect_str("toolPermission", _VALID_TOOL_PERMISSIONS)) is not None:
        setting.tool_permission = v

    if "permissions" in raw_map and raw_map["permissions"] is not None:
        raw_perms = raw_map["permissions"]
        if not isinstance(raw_perms, dict):
            errors.append(f"permissions: expected object, got {type(raw_perms).__name__}")
        else:
            perms_obj, perm_errors = PermissionsSetting.from_dict(raw_perms)
            errors.extend(perm_errors)
            setting.permissions = perms_obj

    if (v := _expect_nonneg_int("historySize")) is not None:
        setting.history_size = v
    if (v := _expect_str("model")) is not None:
        setting.model = v

    if "customModelsConfig" in raw_map and raw_map["customModelsConfig"] is not None:
        cmc = raw_map["customModelsConfig"]
        if not isinstance(cmc, dict):
            errors.append("customModelsConfig: expected object")
        else:
            setting.custom_models_config = copy.deepcopy(cmc)

    if (v := _expect_nonneg_int("contextWindow")) is not None:
        setting.context_window = v
    if (v := _expect_bool("notifications")) is not None:
        setting.notifications = v
    if (v := _expect_str("editor")) is not None:
        setting.editor = v or DEFAULT_EDITOR
    if (v := _expect_str("editorMode", _VALID_EDITOR_MODES)) is not None:
        setting.editor_mode = v
    if (v := _expect_bool("vimInsertFirst")) is not None:
        setting.vim_insert_first = v
    if (v := _expect_bool("clearScrollbackOnResize")) is not None:
        setting.clear_scrollback_on_resize = v
    if (v := _expect_bool("allowNonWorkspaceAccess")) is not None:
        setting.allow_non_workspace_access = v
    if (v := _expect_bool("useTerminalCursor")) is not None:
        setting.use_terminal_cursor = v
    if (v := _expect_str("verbosity", _VALID_VERBOSITY_LEVELS)) is not None:
        setting.verbosity = v
    if (v := _expect_str("artifactReviewPolicy")) is not None:
        setting.artifact_review_policy = v
    if (v := _expect_bool("runningLightSpeed")) is not None:
        setting.running_light_speed = v

    if "statusLine" in raw_map and raw_map["statusLine"] is not None:
        sl = raw_map["statusLine"]
        if not isinstance(sl, (dict, str)):
            errors.append("statusLine: expected object or string")
        else:
            setting.status_line = copy.deepcopy(sl)

    if "title" in raw_map and raw_map["title"] is not None:
        tl = raw_map["title"]
        if not isinstance(tl, (dict, str)):
            errors.append("title: expected object or string")
        else:
            setting.title = copy.deepcopy(tl)

    if (v := _expect_bool("showTips")) is not None:
        setting.show_tips = v
    if (v := _expect_bool("enableTerminalSandbox")) is not None:
        setting.enable_terminal_sandbox = v

    for obj_key, attr_name in (("sandboxProxy", "sandbox_proxy"), ("gcp", "gcp")):
        if obj_key in raw_map and raw_map[obj_key] is not None:
            val = raw_map[obj_key]
            if not isinstance(val, dict):
                errors.append(f"{obj_key}: expected object")
            else:
                setattr(setting, attr_name, copy.deepcopy(val))

    if (v := _expect_bool("showFeedbackSurvey")) is not None:
        setting.show_feedback_survey = v
    if (v := _expect_bool("useG1Credits")) is not None:
        setting.use_g1_credits = v
    if (v := _expect_bool("hideActiveItems")) is not None:
        setting.hide_active_items = v
    if (v := _expect_str("agentMode")) is not None:
        setting.agent_mode = v
    if (v := _expect_bool("copyOnSelect")) is not None:
        setting.copy_on_select = v
    if (v := _expect_bool("disableSlashCommands")) is not None:
        setting.disable_slash_commands = v
    if (v := _expect_str("modelProvider")) is not None:
        setting.model_provider = v
    if (v := _expect_str("pickerGrouping")) is not None:
        setting.picker_grouping = v
    if (v := _expect_str("queuedMessages")) is not None:
        setting.queued_messages = v

    if "autoCompact" in raw_map and raw_map["autoCompact"] is not None:
        ac_raw = raw_map["autoCompact"]
        if isinstance(ac_raw, int) and not isinstance(ac_raw, bool) and ac_raw > 0:
            setting.auto_compact = str(ac_raw)
        elif isinstance(ac_raw, str):
            normalized_ac, ok = auto_compact_override(ac_raw)
            if not ok and ac_raw.strip():
                errors.append(f"autoCompact: invalid value {ac_raw!r}")
            else:
                setting.auto_compact = normalized_ac
        else:
            errors.append(f"autoCompact: expected string or positive integer, got {type(ac_raw).__name__}")

    # OpsCloud structured fields
    if "enabledPlugins" in raw_map and raw_map["enabledPlugins"] is not None:
        ep = raw_map["enabledPlugins"]
        if not isinstance(ep, dict):
            errors.append("enabledPlugins: expected object mapping plugin IDs to booleans")
        else:
            parsed_ep: dict[str, bool] = {}
            for k, v in ep.items():
                if not isinstance(k, str) or not isinstance(v, bool):
                    errors.append(f"enabledPlugins.{k}: expected boolean")
                else:
                    parsed_ep[k] = v
            setting.enabled_plugins = parsed_ep

    if (v := _expect_str("reasoningEffort")) is not None:
        setting.reasoning_effort = v
    if (v := _expect_str("awsProfile")) is not None:
        setting.aws_profile = v
    if (v := _expect_str("awsRegion")) is not None:
        setting.aws_region = v

    if "agentPool" in raw_map and raw_map["agentPool"] is not None:
        ap = raw_map["agentPool"]
        if not isinstance(ap, dict):
            errors.append("agentPool: expected object")
        else:
            parsed_ap: dict[str, str] = {}
            for k, v in ap.items():
                if isinstance(k, str) and isinstance(v, str):
                    parsed_ap[k] = v
                else:
                    errors.append(f"agentPool.{k}: expected string")
            setting.agent_pool = parsed_ap

    # Preserve unknown fields verbatim
    unknown: dict[str, Any] = {}
    for k, v in raw_map.items():
        if k not in _KNOWN_JSON_KEYS:
            unknown[k] = copy.deepcopy(v)
    setting.unknown_fields = unknown

    return setting, errors


def auto_compact_override(raw_value: str) -> tuple[str, bool]:
    """Validate and normalize an ``autoCompact`` setting string.

    - Trims whitespace.
    - If ending with ``"%"``, parses the unsigned integer percentage ``1..100``
      and returns ``("<pct>%", True)`` (or canonical fraction/percentage).
    - Otherwise parses a positive 32-bit unsigned integer token count and
      returns ``("<tokens>", True)``.
    - Returns ``("", False)`` for empty or invalid inputs.
    """
    if not isinstance(raw_value, str):
        return "", False
    s = raw_value.strip()
    if not s:
        return "", False
    if s.endswith("%"):
        num_str = s[:-1]
        if not num_str.isdigit():
            return "", False
        val = int(num_str, 10)
        if val <= 0 or val > 100:
            return "", False
        return f"{val}%", True
    if not s.isdigit():
        return "", False
    val = int(s, 10)
    if val <= 0 or val > 0xFFFFFFFF:
        return "", False
    return str(val), True


def validate_cli_settings(raw_content: bytes | str) -> CliSetting:
    """Parse and validate raw JSON content into a ``CliSetting``.

    - Raises ``SettingsMalformedError`` if JSON syntax is invalid or root is not an object.
    - Raises ``SettingsValidationError`` if any known field fails validation.
    """
    text = raw_content.decode("utf-8") if isinstance(raw_content, bytes) else raw_content
    if not text.strip():
        return default_cli_settings()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SettingsMalformedError(f"settings file is malformed: {exc}") from exc

    if not isinstance(parsed, dict):
        raise SettingsMalformedError("settings file is malformed: root value must be a JSON object")

    setting, errors = parse_settings_fields(parsed)
    if errors:
        raise SettingsValidationError(errors)

    return setting


def _merge_string_list(
    disk_list: list[str],
    target_list: list[str],
    base_list: list[str] | None,
) -> list[str]:
    """Three-way merge for ordered string lists (e.g. ``trustedWorkspaces``)."""
    if base_list is None:
        combined = list(disk_list)
        for item in target_list:
            if item not in combined:
                combined.append(item)
        return combined

    base_set = set(base_list)
    target_set = set(target_list)
    added_by_target = [x for x in target_list if x not in base_set]
    removed_by_target = base_set - target_set

    result: list[str] = []
    for item in disk_list:
        if item not in removed_by_target and item not in result:
            result.append(item)
    for item in added_by_target:
        if item not in result:
            result.append(item)
    return result


def merge_cli_settings(
    disk: CliSetting,
    target: CliSetting,
    base: CliSetting | None = None,
) -> CliSetting:
    """Three-way merge of ``disk``, ``target``, and ``base`` ``CliSetting`` snapshots.

    - Preserves concurrent external modifications on ``disk`` for fields that
      ``target`` did not change relative to ``base``.
    - Applies ``target``'s modifications for fields that changed relative to ``base``.
    """
    effective_base = base if base is not None else default_cli_settings()
    merged = copy.deepcopy(disk)

    scalar_fields = (
        "color_scheme",
        "alt_screen_mode",
        "tool_permission",
        "history_size",
        "model",
        "custom_models_config",
        "context_window",
        "notifications",
        "editor",
        "editor_mode",
        "vim_insert_first",
        "clear_scrollback_on_resize",
        "allow_non_workspace_access",
        "use_terminal_cursor",
        "verbosity",
        "artifact_review_policy",
        "running_light_speed",
        "status_line",
        "title",
        "show_tips",
        "enable_terminal_sandbox",
        "sandbox_proxy",
        "gcp",
        "show_feedback_survey",
        "use_g1_credits",
        "hide_active_items",
        "agent_mode",
        "copy_on_select",
        "disable_slash_commands",
        "model_provider",
        "picker_grouping",
        "queued_messages",
        "auto_compact",
        "reasoning_effort",
        "aws_profile",
        "aws_region",
    )

    for attr in scalar_fields:
        target_val = getattr(target, attr)
        base_val = getattr(effective_base, attr)
        if target_val != base_val:
            setattr(merged, attr, copy.deepcopy(target_val))

    # Merge trustedWorkspaces
    merged.trusted_workspaces = _merge_string_list(
        disk.trusted_workspaces,
        target.trusted_workspaces,
        base.trusted_workspaces if base is not None else None,
    )

    # Merge permissions
    if target.permissions != effective_base.permissions:
        if target.permissions is None:
            merged.permissions = None
        else:
            disk_p = disk.permissions or PermissionsSetting()
            base_p = (base.permissions if base is not None else None)
            merged.permissions = PermissionsSetting(
                allow=_merge_string_list(
                    disk_p.allow,
                    target.permissions.allow,
                    base_p.allow if base_p is not None else None,
                ),
                deny=_merge_string_list(
                    disk_p.deny,
                    target.permissions.deny,
                    base_p.deny if base_p is not None else None,
                ),
                ask=_merge_string_list(
                    disk_p.ask,
                    target.permissions.ask,
                    base_p.ask if base_p is not None else None,
                ),
            )

    # Merge enabledPlugins map
    base_ep = effective_base.enabled_plugins
    if target.enabled_plugins != base_ep:
        merged_ep = dict(disk.enabled_plugins)
        if base is not None:
            for k in set(base_ep) - set(target.enabled_plugins):
                merged_ep.pop(k, None)
        for k, v in target.enabled_plugins.items():
            if base is None or base_ep.get(k) != v:
                merged_ep[k] = v
        merged.enabled_plugins = merged_ep

    # Merge agentPool map
    base_ap = effective_base.agent_pool
    if target.agent_pool != base_ap:
        merged_ap = dict(disk.agent_pool)
        if base is not None:
            for k in set(base_ap) - set(target.agent_pool):
                merged_ap.pop(k, None)
        for k, v in target.agent_pool.items():
            if base is None or base_ap.get(k) != v:
                merged_ap[k] = v
        merged.agent_pool = merged_ap

    # Merge unknown_fields
    base_unk = effective_base.unknown_fields
    merged_unk = copy.deepcopy(disk.unknown_fields)
    if base is not None:
        for k in set(base_unk) - set(target.unknown_fields):
            merged_unk.pop(k, None)
    for k, v in target.unknown_fields.items():
        if base is None or base_unk.get(k) != v:
            merged_unk[k] = copy.deepcopy(v)
    merged.unknown_fields = merged_unk

    return merged


def atomic_write_settings(path: Path, data: bytes | str) -> None:
    """Atomically write settings bytes to ``path`` using ``<path>.<uuid>.tmp`` (mode 0600).

    - Ensures parent directory exists with mode ``0755``.
    - Writes to ``<path>.<uuid4>.tmp`` with permissions ``0600``.
    - Flushes, fsyncs, and atomically replaces ``path``.
    """
    raw_bytes = data.encode("utf-8") if isinstance(data, str) else data
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    tmp_path = path.parent / f"{path.name}.{uuid.uuid4()}.tmp"

    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw_bytes)
            f.flush()
            with contextlib.suppress(OSError):
                os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise


def has_workspace_dir(workspace_dirs: Sequence[str], target_dir: str | Path) -> bool:
    """Check whether ``target_dir`` is present in ``workspace_dirs``."""
    raw = str(target_dir).strip() if target_dir is not None else ""
    if not raw:
        return False
    cleaned = resolve_path_no_slash(raw)
    for d in workspace_dirs:
        if d == raw or d == cleaned or resolve_path_no_slash(d) == cleaned:
            return True
    return False


class CliSettingsManager:
    """Stateful manager for CLI settings and workspace trust.

    Manages the full lifecycle of ``~/.opscloud/settings.json`` and workspace trust:
    - ``init_cli_settings``
    - ``read_cli_settings``
    - ``write_cli_settings``
    - ``quarantine_cli_settings``
    - ``init_workspace_dirs``
    - ``is_startup_workspace_trusted``
    - ``workspace_trusted``
    - ``trust_workspace``
    """

    def __init__(self, settings_path: Path | None = None) -> None:
        self._explicit_path = settings_path
        self._lock = threading.RLock()
        self.settings: CliSetting = default_cli_settings()
        self._base_settings: CliSetting | None = None
        self.settings_load_error: Exception | None = None
        self.startup_workspace_dir: str = ""
        self.workspace_dirs: list[str] = []
        self._initialized: bool = False

    @property
    def settings_path(self) -> Path:
        return self._explicit_path if self._explicit_path is not None else resolve_cli_settings_path()

    def init_cli_settings(self) -> CliSetting:
        """Load and validate ``settings.json`` at startup.

        If reading/validation fails, records ``self.settings_load_error`` so the
        UI can transition to the settings error recovery flow.
        """
        with self._lock:
            self.settings_load_error = None
            try:
                loaded = self.read_cli_settings()
                self.settings = loaded
                self._base_settings = copy.deepcopy(loaded)
            except Exception as exc:
                logger.warning("Failed to load CLI settings from %s: %s", self.settings_path, exc)
                self.settings_load_error = exc
                self.settings = default_cli_settings()
                self._base_settings = None
            self._initialized = True
            return self.settings

    def get_settings(self) -> CliSetting:
        """Return the active in-memory CLI settings (initializing from disk if needed)."""
        with self._lock:
            if not self._initialized:
                return self.init_cli_settings()
            return self.settings

    def read_cli_settings(self) -> CliSetting:
        """Read, validate, and normalize ``settings.json`` from disk.

        - Returns ``default_cli_settings()`` if the file does not exist.
        - Validates JSON syntax and field schema.
        - If the on-disk bytes are valid but not in canonical sparse form,
          rewrites the normalized sparse representation to disk.
        """
        with self._lock:
            path = self.settings_path
            if not path.exists():
                default_s = default_cli_settings()
                self.settings = default_s
                self._base_settings = copy.deepcopy(default_s)
                return default_s

            raw_bytes = path.read_bytes()
            if not raw_bytes.strip():
                default_s = default_cli_settings()
                self.settings = default_s
                self._base_settings = copy.deepcopy(default_s)
                return default_s

            setting = validate_cli_settings(raw_bytes)
            sparse_bytes = setting.marshal_sparse()

            if raw_bytes != sparse_bytes:
                with contextlib.suppress(OSError):
                    atomic_write_settings(path, sparse_bytes)

            self.settings = setting
            self._base_settings = copy.deepcopy(setting)
            self.settings_load_error = None
            return copy.deepcopy(setting)

    def _parse_disk_settings_for_write(self) -> CliSetting:
        """Read existing disk settings prior to a write, refusing to overwrite malformed JSON."""
        path = self.settings_path
        if not path.exists():
            return default_cli_settings()
        try:
            raw_bytes = path.read_bytes()
        except FileNotFoundError:
            return default_cli_settings()
        except OSError as exc:
            raise SettingsError(f"refusing to overwrite {path}: {exc}") from exc

        if not raw_bytes.strip():
            return default_cli_settings()

        try:
            parsed = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SettingsMalformedError(
                f"refusing to overwrite {path}: settings file is malformed: {exc}"
            ) from exc

        if not isinstance(parsed, dict):
            raise SettingsMalformedError(
                f"refusing to overwrite {path}: root value must be a JSON object"
            )

        disk_setting, _ = parse_settings_fields(parsed)
        return disk_setting

    def write_cli_settings(self, target: CliSetting) -> CliSetting:
        """Perform a 3-way merge with disk and atomically write sparse ``settings.json``."""
        with self._lock:
            if target.permissions is not None:
                for cat_name, rules in (
                    ("allow", target.permissions.allow),
                    ("deny", target.permissions.deny),
                    ("ask", target.permissions.ask),
                ):
                    for rule in rules:
                        if not is_valid_unified_rule(rule):
                            raise SettingsValidationError(
                                [f"permissions.{cat_name}: invalid unified rule {rule!r}"]
                            )

            disk_setting = self._parse_disk_settings_for_write()
            merged = merge_cli_settings(disk_setting, target, self._base_settings)
            sparse_bytes = merged.marshal_sparse()
            atomic_write_settings(self.settings_path, sparse_bytes)

            self.settings = copy.deepcopy(merged)
            self._base_settings = copy.deepcopy(merged)
            self.settings_load_error = None
            return copy.deepcopy(merged)

    def quarantine_cli_settings(self) -> Path | None:
        """Quarantine a corrupted ``settings.json`` to ``<path>.corrupted.<unix_ms>`` and reset."""
        with self._lock:
            path = self.settings_path
            quarantine_path: Path | None = None
            if path.exists():
                raw_bytes = path.read_bytes()
                ts_ms = int(time.time() * 1000)
                quarantine_path = path.parent / f"{path.name}.corrupted.{ts_ms}"
                atomic_write_settings(quarantine_path, raw_bytes)
                path.unlink(missing_ok=True)

            self.settings = default_cli_settings()
            self._base_settings = copy.deepcopy(self.settings)
            self.settings_load_error = None
            return quarantine_path

    def is_workspace_in_any_project(self, ws_dir: str | Path) -> bool:
        """Check if ``ws_dir`` belongs to any saved project definition.

        Scans ``~/.opscloud/projects/*.json`` (and ``~/.gemini/projects/*.json``)
        for any project whose ``workspaces`` list contains ``ws_dir``.
        """
        cleaned = resolve_path_no_slash(ws_dir)
        if not cleaned:
            return False

        candidate_dirs = [paths.DATA_DIR / "projects"]
        gemini_projects = Path.home() / ".gemini" / "projects"
        if gemini_projects not in candidate_dirs:
            candidate_dirs.append(gemini_projects)

        for proj_dir in candidate_dirs:
            if not proj_dir.is_dir():
                continue
            try:
                for entry in proj_dir.iterdir():
                    if entry.is_dir() or entry.suffix != ".json":
                        continue
                    with contextlib.suppress(Exception):
                        data = json.loads(entry.read_text(encoding="utf-8"))
                        if isinstance(data, dict):
                            workspaces = data.get("workspaces", [])
                            if isinstance(workspaces, list):
                                for ws in workspaces:
                                    if isinstance(ws, str) and (
                                        ws == cleaned or resolve_path_no_slash(ws) == cleaned
                                    ):
                                        return True
            except OSError:
                continue
        return False

    def is_startup_workspace_trusted(self, ws_dir: str | Path | None = None) -> bool:
        """Check whether the startup workspace directory is trusted."""
        with self._lock:
            if not self._initialized:
                self.init_cli_settings()
            target = resolve_path_no_slash(ws_dir or self.startup_workspace_dir or Path.cwd())
            if not target:
                return True
            if self.settings.is_trusted_workspace(target):
                return True
            if self.is_workspace_in_any_project(target):
                return True
            return False

    def init_workspace_dirs(self, startup_dir: str | Path | None = None) -> bool:
        """Initialize startup workspace directory and add to active workspace_dirs if trusted.

        Returns ``True`` if the startup workspace is trusted, ``False`` if trust prompt is needed.
        """
        with self._lock:
            if not self._initialized:
                self.init_cli_settings()
            cleaned = resolve_path_no_slash(startup_dir or Path.cwd())
            self.startup_workspace_dir = cleaned
            if not cleaned:
                return True
            if self.is_startup_workspace_trusted(cleaned):
                if not has_workspace_dir(self.workspace_dirs, cleaned):
                    self.workspace_dirs.append(cleaned)
                return True
            return False

    def workspace_trusted(self, ws_dir: str | Path | None = None) -> bool:
        """Return whether the active/startup workspace is currently trusted.

        Checks ``has_workspace_dir(self.workspace_dirs, target)`` or
        ``self.settings.is_trusted_workspace(target)``.
        """
        with self._lock:
            if not self._initialized:
                self.init_cli_settings()
            target = resolve_path_no_slash(ws_dir or self.startup_workspace_dir)
            if not target:
                return True
            if has_workspace_dir(self.workspace_dirs, target):
                return True
            if self.settings.is_trusted_workspace(target):
                return True
            return False

    def add_workspace_dir(self, ws_dir: str | Path) -> str:
        """Add ``ws_dir`` to active ``workspace_dirs`` in the session store."""
        with self._lock:
            cleaned = resolve_path_no_slash(ws_dir)
            if cleaned and not has_workspace_dir(self.workspace_dirs, cleaned):
                self.workspace_dirs.append(cleaned)
            return cleaned

    def trust_workspace(self, ws_dir: str | Path | None = None) -> CliSetting:
        """Grant trust to ``ws_dir`` and persist it in ``trustedWorkspaces`` in ``settings.json``.

        1. Adds ``ws_dir`` to ``self.workspace_dirs``.
        2. Appends ``ws_dir`` to ``self.settings.trusted_workspaces`` if not already present.
        3. Writes updated settings via ``write_cli_settings`` (3-way merge + atomic write).
        """
        with self._lock:
            if not self._initialized:
                self.init_cli_settings()
            target = resolve_path_no_slash(ws_dir or self.startup_workspace_dir or Path.cwd())
            if not target:
                return self.settings

            self.add_workspace_dir(target)
            if not self.settings.is_trusted_workspace(target):
                updated = copy.deepcopy(self.settings)
                updated.trusted_workspaces.append(target)
                return self.write_cli_settings(updated)
            return copy.deepcopy(self.settings)

    def revoke_workspace_trust(self, ws_dir: str | Path) -> bool:
        """Remove ``ws_dir`` from ``trustedWorkspaces`` and active ``workspace_dirs``."""
        with self._lock:
            if not self._initialized:
                self.init_cli_settings()
            cleaned = resolve_path_no_slash(ws_dir)
            if not cleaned:
                return False

            self.workspace_dirs = [
                d for d in self.workspace_dirs if resolve_path_no_slash(d) != cleaned
            ]
            before = len(self.settings.trusted_workspaces)
            new_trusted = [
                w
                for w in self.settings.trusted_workspaces
                if w != str(ws_dir) and resolve_path_no_slash(w) != cleaned
            ]
            if len(new_trusted) == before:
                return False

            updated = copy.deepcopy(self.settings)
            updated.trusted_workspaces = new_trusted
            self.write_cli_settings(updated)
            return True


_manager_singleton: CliSettingsManager | None = None
_manager_lock = threading.RLock()


def get_cli_settings_manager() -> CliSettingsManager:
    """Return the process-wide ``CliSettingsManager`` singleton."""
    global _manager_singleton
    if _manager_singleton is None:
        with _manager_lock:
            if _manager_singleton is None:
                mgr = CliSettingsManager()
                mgr.init_cli_settings()
                _manager_singleton = mgr
    return _manager_singleton


def reset_cli_settings_manager_for_testing() -> None:
    """Reset the ``CliSettingsManager`` singleton (used by test fixtures)."""
    global _manager_singleton
    with _manager_lock:
        _manager_singleton = None


def is_workspace_trusted(ws_dir: str | Path | None = None) -> bool:
    """Convenience helper to check if ``ws_dir`` (or CWD) is trusted."""
    mgr = get_cli_settings_manager()
    target = ws_dir if ws_dir is not None else (mgr.startup_workspace_dir or Path.cwd())
    return mgr.workspace_trusted(target) or mgr.is_startup_workspace_trusted(target)


def trust_workspace(ws_dir: str | Path | None = None) -> CliSetting:
    """Convenience helper to trust ``ws_dir`` and persist to ``~/.opscloud/settings.json``."""
    return get_cli_settings_manager().trust_workspace(ws_dir)


__all__ = [
    "CliSetting",
    "CliSettingsManager",
    "DEFAULT_EDITOR",
    "PermissionsSetting",
    "SettingsError",
    "SettingsMalformedError",
    "SettingsValidationError",
    "atomic_write_settings",
    "auto_compact_override",
    "default_cli_settings",
    "get_cli_settings_manager",
    "has_workspace_dir",
    "is_valid_unified_rule",
    "is_workspace_trusted",
    "merge_cli_settings",
    "parse_settings_fields",
    "resolve_cli_settings_path",
    "resolve_path_no_slash",
    "reset_cli_settings_manager_for_testing",
    "trust_workspace",
    "validate_cli_settings",
]
