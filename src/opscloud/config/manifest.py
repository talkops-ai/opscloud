"""Configuration manifest and option resolution engine for OpsCloud.

Single source of truth for all configurable settings across OpsCloud:
the options, their types, defaults, env-var names, DB keys, and TOML keys.
"""

from __future__ import annotations

import json
from opscloud.utils.logger import get_logger
import os
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opscloud.config.paths import (
    CONFIG_PATH as DEFAULT_CONFIG_PATH,
    ENV_PREFIX,
)

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)


# ── OptionKind ───────────────────────────────────────────


class OptionKind(Enum):
    """How an option's raw value is coerced to a typed value."""

    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    STR = "str"
    CHOICE = "choice"
    PATH = "path"
    SHELL_LIST = "shell_list"
    SECRET = "secret"
    JSON = "json"


_KIND_TYPE_LABEL: dict[OptionKind, str] = {
    OptionKind.BOOL: "bool",
    OptionKind.INT: "int",
    OptionKind.FLOAT: "float",
    OptionKind.STR: "str",
    OptionKind.CHOICE: "choice",
    OptionKind.PATH: "path",
    OptionKind.SHELL_LIST: "list[str]",
    OptionKind.SECRET: "secret",
    OptionKind.JSON: "json",
}


# ── ConfigOption ─────────────────────────────────────────


@dataclass(frozen=True)
class ConfigOption:
    """One user-tunable configuration option with DB, env, and TOML mappings."""

    key: str
    """Canonical dotted identifier (e.g. 'models.name', 'aws.region')."""

    group: str
    """Human-readable grouping for the config UI and store categorization."""

    summary: str
    """One-line description."""

    kind: OptionKind
    """How values are coerced."""

    db_key: str = ""
    """Key used in DB config_store (defaults to env_var or key)."""

    default: Any = None
    """Typed default value."""

    env_var: str | None = None
    """Environment variable name (without OPSCLOUD_ prefix)."""

    toml_keys: tuple[str, ...] | None = None
    """Key path in config.toml (e.g. ('aws', 'region'))."""

    settings_field: str | None = None
    """Settings dataclass attribute name."""

    redacted: bool = False
    """Mask value in display/logs."""

    choices: tuple[str, ...] | None = None
    """Valid values when kind is CHOICE."""

    def __post_init__(self) -> None:
        if not self.db_key:
            # Default db_key to env_var or key
            object.__setattr__(self, "db_key", self.env_var or self.key)

    @property
    def type_label(self) -> str:
        return _KIND_TYPE_LABEL[self.kind]

    @property
    def effective_env_var(self) -> str:
        """Return the primary environment variable name for this option."""
        return self.env_var or self.db_key

    @property
    def toml_path(self) -> str | None:
        if not self.toml_keys:
            return None
        *sections, leaf = self.toml_keys
        return f"[{'.'.join(sections)}].{leaf}" if sections else leaf


# ── Coercion & Serialization ─────────────────────────────


def coerce_str_value(kind: OptionKind, raw: str) -> Any:
    """Coerce a raw string value to a typed Python object."""
    if kind == OptionKind.BOOL:
        low = raw.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
        return None
    if kind == OptionKind.INT:
        try:
            return int(raw.strip())
        except ValueError:
            return None
    if kind == OptionKind.FLOAT:
        try:
            return float(raw.strip())
        except ValueError:
            return None
    if kind in (OptionKind.STR, OptionKind.CHOICE, OptionKind.SECRET):
        return raw
    if kind == OptionKind.SHELL_LIST:
        return [cmd.strip() for cmd in raw.split(",") if cmd.strip()]
    if kind == OptionKind.PATH:
        return Path(raw.strip()).expanduser().resolve()
    if kind == OptionKind.JSON:
        try:
            return json.loads(raw.strip())
        except (ValueError, TypeError):
            return None
    return raw


def serialize_typed_value(kind: OptionKind, value: Any) -> str:
    """Serialize a typed Python value to a string for DB or env storage."""
    if value is None:
        return ""
    if kind == OptionKind.BOOL:
        return "true" if value else "false"
    if kind == OptionKind.SHELL_LIST:
        if isinstance(value, (list, tuple, set)):
            return ",".join(str(item) for item in value)
        return str(value)
    if kind == OptionKind.PATH:
        return str(Path(value).expanduser().resolve())
    if kind == OptionKind.JSON:
        return json.dumps(value)
    return str(value)


def _coerce_env(option: ConfigOption, raw: str) -> Any:
    return coerce_str_value(option.kind, raw)


def _coerce_toml(option: ConfigOption, raw: Any) -> Any:
    kind = option.kind
    if kind == OptionKind.BOOL:
        return raw if isinstance(raw, bool) else None
    if kind == OptionKind.INT:
        return raw if isinstance(raw, int) and not isinstance(raw, bool) else None
    if kind == OptionKind.FLOAT:
        return float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None
    if kind in (OptionKind.STR, OptionKind.CHOICE, OptionKind.SECRET):
        return raw if isinstance(raw, str) else None
    if kind == OptionKind.SHELL_LIST:
        if isinstance(raw, str):
            return [cmd.strip() for cmd in raw.split(",") if cmd.strip()]
        if isinstance(raw, list):
            return [str(item) for item in raw if item]
        return None
    if kind == OptionKind.PATH:
        return Path(raw).expanduser() if isinstance(raw, str) else None
    if kind == OptionKind.JSON:
        return raw if isinstance(raw, (dict, list)) else None
    return None


# ── TOML Loading Helpers ─────────────────────────────────


def load_config_toml(path: Path | None = None) -> dict[str, Any]:
    """Safely load and parse TOML configuration."""
    target = path or DEFAULT_CONFIG_PATH
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]

    try:
        with target.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("Could not read config from %s: %s", target, exc)
        return {}


def _toml_lookup(data: dict[str, Any], keys: tuple[str, ...]) -> tuple[bool, Any]:
    """Look up nested keys in a parsed TOML dict."""
    node: Any = data
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return False, None
        node = node[key]
    return True, node


# ── Resolution Engine ────────────────────────────────────


def resolve_from_env(option: ConfigOption) -> tuple[Any, str] | None:
    """Resolve an option value from environment variables with OPSCLOUD_ prefix precedence."""
    env_name = option.effective_env_var
    if not env_name:
        return None

    prefixed = f"{ENV_PREFIX}{env_name}"
    for candidate in (prefixed, env_name):
        raw = os.environ.get(candidate)
        if raw is not None and raw != "":
            coerced = _coerce_env(option, raw)
            if coerced is not None:
                return coerced, f"env ({candidate})"
    return None


def resolve_scalar(
    option: ConfigOption,
    *,
    settings: Any | None = None,
    toml_data: dict[str, Any] | None = None,
) -> tuple[Any, str]:
    """Resolve an option value using the precedence: settings -> env -> toml -> default."""
    if settings is not None and option.settings_field:
        val = getattr(settings, option.settings_field, None)
        if val is not None:
            return val, "settings"

    env_val = resolve_from_env(option)
    if env_val is not None:
        return env_val

    if option.toml_keys:
        if toml_data is None:
            toml_data = load_config_toml()
        found, raw = _toml_lookup(toml_data, option.toml_keys)
        if found:
            coerced = _coerce_toml(option, raw)
            if coerced is not None:
                return coerced, "config.toml"

    return option.default, "default"


# ── Credential Options ───────────────────────────────────

_CREDENTIAL_OPTIONS: tuple[ConfigOption, ...] = (
    # AWS Credentials
    ConfigOption(
        key="credentials.aws_key_id",
        group="Credentials",
        summary="AWS Access Key ID",
        kind=OptionKind.SECRET,
        db_key="AWS_ACCESS_KEY_ID",
        env_var="AWS_ACCESS_KEY_ID",
        settings_field="aws_access_key_id",
        redacted=True,
    ),
    ConfigOption(
        key="credentials.aws_secret_key",
        group="Credentials",
        summary="AWS Secret Access Key",
        kind=OptionKind.SECRET,
        db_key="AWS_SECRET_ACCESS_KEY",
        env_var="AWS_SECRET_ACCESS_KEY",
        settings_field="aws_secret_access_key",
        redacted=True,
    ),
    ConfigOption(
        key="credentials.aws_session_token",
        group="Credentials",
        summary="AWS Session Token",
        kind=OptionKind.SECRET,
        db_key="AWS_SESSION_TOKEN",
        env_var="AWS_SESSION_TOKEN",
        settings_field="aws_session_token",
        redacted=True,
    ),
    # LLM Provider Credentials
    ConfigOption(
        key="credentials.openai",
        group="Credentials",
        summary="OpenAI API Key",
        kind=OptionKind.SECRET,
        db_key="OPENAI_API_KEY",
        env_var="OPENAI_API_KEY",
        settings_field="openai_api_key",
        redacted=True,
    ),
    ConfigOption(
        key="credentials.anthropic",
        group="Credentials",
        summary="Anthropic API Key",
        kind=OptionKind.SECRET,
        db_key="ANTHROPIC_API_KEY",
        env_var="ANTHROPIC_API_KEY",
        settings_field="anthropic_api_key",
        redacted=True,
    ),
    ConfigOption(
        key="credentials.google",
        group="Credentials",
        summary="Google Gemini API Key",
        kind=OptionKind.SECRET,
        db_key="GOOGLE_API_KEY",
        env_var="GOOGLE_API_KEY",
        settings_field="google_api_key",
        redacted=True,
    ),
    ConfigOption(
        key="credentials.groq",
        group="Credentials",
        summary="Groq API Key",
        kind=OptionKind.SECRET,
        db_key="GROQ_API_KEY",
        env_var="GROQ_API_KEY",
        settings_field="groq_api_key",
        redacted=True,
    ),
    ConfigOption(
        key="credentials.deepseek",
        group="Credentials",
        summary="DeepSeek API Key",
        kind=OptionKind.SECRET,
        db_key="DEEPSEEK_API_KEY",
        env_var="DEEPSEEK_API_KEY",
        settings_field="deepseek_api_key",
        redacted=True,
    ),
    ConfigOption(
        key="credentials.tavily",
        group="Credentials",
        summary="Tavily Search API Key",
        kind=OptionKind.SECRET,
        db_key="TAVILY_API_KEY",
        env_var="TAVILY_API_KEY",
        settings_field="tavily_api_key",
        redacted=True,
    ),
    ConfigOption(
        key="credentials.langchain",
        group="Credentials",
        summary="LangSmith / LangChain API Key",
        kind=OptionKind.SECRET,
        db_key="LANGCHAIN_API_KEY",
        env_var="LANGCHAIN_API_KEY",
        settings_field="langchain_api_key",
        redacted=True,
    ),
    ConfigOption(
        key="credentials.typesafe",
        group="Credentials",
        summary="TypeSafe / JEV Security Classifier API Key",
        kind=OptionKind.SECRET,
        db_key="TYPESAFE_API_KEY",
        env_var="TYPESAFE_API_KEY",
        settings_field="typesafe_api_key",
        redacted=True,
    ),
)


# ── Static Configuration Options ─────────────────────────

_STATIC_OPTIONS: tuple[ConfigOption, ...] = (
    # ── Cloud Provider Configuration ──────────────────────
    ConfigOption(
        key="cloud.provider",
        group="Cloud",
        summary="Default cloud provider ('aws', 'azure', 'gcp', 'multi')",
        kind=OptionKind.STR,
        db_key="CLOUD_PROVIDER",
        env_var="OPSCLOUD_CLOUD_PROVIDER",
        toml_keys=("cloud", "provider"),
        settings_field="cloud_provider",
        default="aws",
    ),

    # ── AWS Cloud Configuration ───────────────────────────
    ConfigOption(
        key="aws.region",
        group="AWS",
        summary="Default AWS region",
        kind=OptionKind.STR,
        db_key="AWS_REGION",
        env_var="AWS_REGION",
        toml_keys=("aws", "region"),
        settings_field="aws_region",
        default="us-east-1",
    ),
    ConfigOption(
        key="aws.profile",
        group="AWS",
        summary="Default AWS named profile",
        kind=OptionKind.STR,
        db_key="AWS_PROFILE",
        env_var="AWS_PROFILE",
        toml_keys=("aws", "profile"),
        settings_field="aws_profile",
        default="default",
    ),
    ConfigOption(
        key="aws.role_arn",
        group="AWS",
        summary="Optional IAM role ARN to assume",
        kind=OptionKind.STR,
        db_key="AWS_ROLE_ARN",
        env_var="AWS_ROLE_ARN",
        toml_keys=("aws", "role_arn"),
        settings_field="aws_role_arn",
    ),
    ConfigOption(
        key="aws.default_region",
        group="AWS",
        summary="AWS default region alias",
        kind=OptionKind.STR,
        db_key="AWS_DEFAULT_REGION",
        env_var="AWS_DEFAULT_REGION",
        toml_keys=("aws", "default_region"),
    ),
    ConfigOption(
        key="aws.default_profile",
        group="AWS",
        summary="AWS default profile alias",
        kind=OptionKind.STR,
        db_key="AWS_DEFAULT_PROFILE",
        env_var="AWS_DEFAULT_PROFILE",
        toml_keys=("aws", "default_profile"),
    ),
    # ── Models ───────────────────────────────────────────
    ConfigOption(
        key="models.name",
        group="Models",
        summary="Active LLM model specification",
        kind=OptionKind.STR,
        db_key="MODEL",
        env_var="MODEL",
        toml_keys=("models", "default"),
        settings_field="model_name",
        default=None,
    ),
    ConfigOption(
        key="models.provider",
        group="Models",
        summary="Active model provider",
        kind=OptionKind.STR,
        db_key="MODEL_PROVIDER",
        env_var="MODEL_PROVIDER",
        settings_field="model_provider",
        default=None,
    ),
    ConfigOption(
        key="models.reasoning_effort",
        group="Models",
        summary="Reasoning effort level (low/medium/high/off)",
        kind=OptionKind.CHOICE,
        db_key="REASONING_EFFORT",
        env_var="REASONING_EFFORT",
        toml_keys=("effort", "default"),
        settings_field="reasoning_effort",
        default=None,
        choices=("low", "medium", "high", "off", "none"),
    ),
    ConfigOption(
        key="models.context_limit",
        group="Models",
        summary="Model context window token limit",
        kind=OptionKind.INT,
        db_key="MODEL_CONTEXT_LIMIT",
        env_var="MODEL_CONTEXT_LIMIT",
        settings_field="model_context_limit",
        default=128_000,
    ),
    # ── Security & Approvals ──────────────────────────────
    ConfigOption(
        key="security.approval_mode",
        group="Security",
        summary="Action approval mode (manual/auto/plan)",
        kind=OptionKind.CHOICE,
        db_key="APPROVAL_MODE",
        env_var="APPROVAL_MODE",
        settings_field="approval_mode",
        default="manual",
        choices=("manual", "auto", "plan"),
    ),
    ConfigOption(
        key="shell.allow_list",
        group="Security",
        summary="Shell commands allowed without confirmation",
        kind=OptionKind.SHELL_LIST,
        db_key="SHELL_ALLOW_LIST",
        env_var="SHELL_ALLOW_LIST",
        settings_field="shell_allow_list",
    ),
    # ── Sandbox ───────────────────────────────────────────
    ConfigOption(
        key="sandbox.provider",
        group="Sandbox",
        summary="Execution sandbox provider (local/daytona/modal/runloop)",
        kind=OptionKind.CHOICE,
        db_key="SANDBOX_PROVIDER",
        env_var="SANDBOX_PROVIDER",
        toml_keys=("sandboxes", "default"),
        settings_field="sandbox_provider",
        default="local",
        choices=("local", "daytona", "modal", "runloop"),
    ),
    ConfigOption(
        key="sandboxes.sync_workspace",
        group="Sandbox",
        summary="Whether to synchronize workspace files into the sandbox",
        kind=OptionKind.BOOL,
        toml_keys=("sandboxes", "sync_workspace"),
        default=True,
    ),
    # ── Code Interpreter ──────────────────────────────────
    ConfigOption(
        key="interpreter.enable_interpreter",
        group="Interpreter",
        summary="Enable local Python code interpreter tool execution",
        kind=OptionKind.BOOL,
        db_key="ENABLE_INTERPRETER",
        toml_keys=("interpreter", "enable_interpreter"),
        settings_field="enable_interpreter",
        default=True,
    ),
    ConfigOption(
        key="interpreter.timeout_seconds",
        group="Interpreter",
        summary="Execution timeout in seconds for interpreter commands",
        kind=OptionKind.INT,
        db_key="INTERPRETER_TIMEOUT_SECONDS",
        toml_keys=("interpreter", "timeout_seconds"),
        settings_field="interpreter_timeout_seconds",
        default=30,
    ),
    ConfigOption(
        key="interpreter.memory_limit_mb",
        group="Interpreter",
        summary="Memory limit in megabytes for interpreter runtime",
        kind=OptionKind.INT,
        db_key="INTERPRETER_MEMORY_LIMIT_MB",
        toml_keys=("interpreter", "memory_limit_mb"),
        settings_field="interpreter_memory_limit_mb",
        default=64,
    ),
    ConfigOption(
        key="interpreter.max_ptc_calls",
        group="Interpreter",
        summary="Maximum allowed programmatic tool calls per turn",
        kind=OptionKind.INT,
        db_key="INTERPRETER_MAX_PTC_CALLS",
        toml_keys=("interpreter", "max_ptc_calls"),
        settings_field="interpreter_max_ptc_calls",
        default=50,
    ),
    ConfigOption(
        key="interpreter.max_result_chars",
        group="Interpreter",
        summary="Maximum character limit for tool execution output",
        kind=OptionKind.INT,
        db_key="INTERPRETER_MAX_RESULT_CHARS",
        toml_keys=("interpreter", "max_result_chars"),
        settings_field="interpreter_max_result_chars",
        default=50000,
    ),
    ConfigOption(
        key="interpreter.ptc",
        group="Interpreter",
        summary="Programmatic tool call permission level ('safe'/'all'/comma-list)",
        kind=OptionKind.STR,
        db_key="INTERPRETER_PTC",
        toml_keys=("interpreter", "ptc"),
        settings_field="interpreter_ptc",
        default="safe",
    ),
    ConfigOption(
        key="interpreter.ptc_acknowledge_unsafe",
        group="Interpreter",
        summary="Acknowledge execution of unsafe programmatic tool calls",
        kind=OptionKind.BOOL,
        db_key="INTERPRETER_PTC_ACKNOWLEDGE_UNSAFE",
        toml_keys=("interpreter", "ptc_acknowledge_unsafe"),
        settings_field="interpreter_ptc_acknowledge_unsafe",
        default=False,
    ),
    # ── Checkpointing & Storage ───────────────────────────
    ConfigOption(
        key="checkpoint.backend",
        group="Storage",
        summary="State checkpointer backend (sqlite/postgres)",
        kind=OptionKind.CHOICE,
        db_key="CHECKPOINT_BACKEND",
        env_var="CHECKPOINT_BACKEND",
        settings_field="checkpoint_backend",
        default="sqlite",
        choices=("sqlite", "postgres"),
    ),
    ConfigOption(
        key="checkpoint.postgres_uri",
        group="Storage",
        summary="PostgreSQL connection URI for multi-agent checkpoints",
        kind=OptionKind.SECRET,
        db_key="POSTGRES_URI",
        env_var="POSTGRES_URI",
        settings_field="postgres_uri",
        redacted=True,
    ),
    # ── Tracing & LangSmith ───────────────────────────────
    ConfigOption(
        key="tracing.enabled",
        group="Tracing",
        summary="Enable LangSmith / LangChain distributed tracing",
        kind=OptionKind.BOOL,
        db_key="LANGCHAIN_TRACING_V2",
        env_var="LANGCHAIN_TRACING_V2",
        settings_field="langchain_tracing",
        default=False,
    ),
    ConfigOption(
        key="tracing.project",
        group="Tracing",
        summary="LangSmith project name for traces",
        kind=OptionKind.STR,
        db_key="LANGCHAIN_PROJECT",
        env_var="LANGCHAIN_PROJECT",
        settings_field="langchain_project",
        default="opscloud",
    ),
    ConfigOption(
        key="tracing.endpoint",
        group="Tracing",
        summary="LangSmith API endpoint",
        kind=OptionKind.STR,
        db_key="LANGCHAIN_ENDPOINT",
        env_var="LANGCHAIN_ENDPOINT",
        settings_field="langchain_endpoint",
        default="https://api.smith.langchain.com",
    ),
    # ── UI & Display ──────────────────────────────────────
    ConfigOption(
        key="display.theme",
        group="Display",
        summary="Active terminal UI color theme",
        kind=OptionKind.STR,
        db_key="THEME",
        env_var="OPSCLOUD_THEME",
        toml_keys=("ui", "theme"),
        settings_field="theme",
        default="dark",
    ),
    ConfigOption(
        key="display.show_timestamps",
        group="Display",
        summary="Show timestamps on messages",
        kind=OptionKind.BOOL,
        db_key="SHOW_TIMESTAMPS",
        toml_keys=("ui", "show_timestamps"),
        settings_field="show_timestamps",
        default=True,
    ),
    ConfigOption(
        key="display.auto_scroll",
        group="Display",
        summary="Auto-scroll to newest messages",
        kind=OptionKind.BOOL,
        db_key="AUTO_SCROLL",
        toml_keys=("ui", "auto_scroll"),
        settings_field="auto_scroll",
        default=True,
    ),
    ConfigOption(
        key="display.show_turn_duration",
        group="Display",
        summary="Display elapsed execution time per turn",
        kind=OptionKind.BOOL,
        db_key="SHOW_TURN_DURATION",
        toml_keys=("ui", "show_turn_duration"),
        settings_field="show_turn_duration",
        default=True,
    ),
    ConfigOption(
        key="display.notifications_enabled",
        group="Display",
        summary="Enable desktop/terminal notifications",
        kind=OptionKind.BOOL,
        db_key="NOTIFICATIONS_ENABLED",
        toml_keys=("ui", "notifications_enabled"),
        settings_field="notifications_enabled",
        default=True,
    ),
    ConfigOption(
        key="display.verbose_output",
        group="Display",
        summary="Show verbose output in messages",
        kind=OptionKind.BOOL,
        db_key="VERBOSE_OUTPUT",
        toml_keys=("ui", "verbose_output"),
        settings_field="verbose_output",
        default=False,
    ),
    ConfigOption(
        key="display.auto_compact",
        group="Display",
        summary="Automatically compact conversation context",
        kind=OptionKind.BOOL,
        db_key="AUTO_COMPACT",
        toml_keys=("ui", "auto_compact"),
        settings_field="auto_compact",
        default=False,
    ),
    # ── Project & Workspace ───────────────────────────────
    ConfigOption(
        key="project.root",
        group="Project",
        summary="Project root directory",
        kind=OptionKind.PATH,
        db_key="PROJECT_ROOT",
        env_var="PROJECT_ROOT",
        toml_keys=("project", "root"),
        settings_field="project_root",
    ),
    ConfigOption(
        key="skills.extra_dirs",
        group="Project",
        summary="Additional directories to search for custom skills",
        kind=OptionKind.STR,
        db_key="EXTRA_SKILLS_DIRS",
        env_var="EXTRA_SKILLS_DIRS",
        settings_field="extra_skills_dirs",
    ),
)


# ── Manifest Query Surface ───────────────────────────────


@lru_cache(maxsize=1)
def get_config_options() -> tuple[ConfigOption, ...]:
    """Return all canonical configuration options."""
    return _CREDENTIAL_OPTIONS + _STATIC_OPTIONS


def get_option(key: str) -> ConfigOption | None:
    """Look up a configuration option by canonical key (e.g. 'aws.region')."""
    return {opt.key: opt for opt in get_config_options()}.get(key)


def get_option_by_db_key(db_key: str) -> ConfigOption | None:
    """Look up a configuration option by its database key (e.g. 'AWS_REGION')."""
    return {opt.db_key: opt for opt in get_config_options()}.get(db_key)


def option_keys() -> tuple[str, ...]:
    """Return all canonical option keys in declaration order."""
    return tuple(opt.key for opt in get_config_options())


def iter_groups() -> tuple[str, ...]:
    """Return all unique option groups in declaration order."""
    seen: set[str] = set()
    groups: list[str] = []
    for opt in get_config_options():
        if opt.group not in seen:
            seen.add(opt.group)
            groups.append(opt.group)
    return tuple(groups)


__all__ = [
    "ConfigOption",
    "OptionKind",
    "coerce_str_value",
    "get_config_options",
    "get_option",
    "get_option_by_db_key",
    "iter_groups",
    "load_config_toml",
    "option_keys",
    "resolve_from_env",
    "resolve_scalar",
    "serialize_typed_value",
]
