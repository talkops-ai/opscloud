"""Centralized filesystem-path definitions for opscloud.

This module is the **single source of truth** for every directory and file
opscloud reads or writes at runtime. It is intentionally dependency-free (no
sibling-module imports, no third-party packages) so any module can import
from it without triggering circular or expensive imports.
"""

from __future__ import annotations

import errno
from opscloud.utils.logger import get_logger
from enum import StrEnum
from pathlib import Path
from typing import Final

logger = get_logger(__name__)

# ── Constants ────────────────────────────────────────────

ENV_PREFIX: Final[str] = "OPSCLOUD_"
"""All opscloud-specific env vars use this prefix."""

DEFAULT_AGENT_NAME: Final[str] = "opscloud"
"""Default agent / assistant identifier when no -a flag is given."""

# ── Root directories ─────────────────────────────────────

DATA_DIR: Final[Path] = Path.home() / ".opscloud"
"""``~/.opscloud/`` — Top-level user data directory."""

OPSCLOUD_HOME: Final[Path] = DATA_DIR
USER_HOME: Final[Path] = Path.home()

STATE_DIR: Final[Path] = DATA_DIR / ".state"
"""``~/.opscloud/.state/`` — Machine-managed state (never hand-edit)."""

AGENTS_SHARED_DIR: Final[Path] = Path.home() / ".agents"
"""``~/.agents/`` — Tool-agnostic data (shared across AI CLIs)."""

# ── User-facing config files (human-editable) ────────────

CONFIG_PATH: Final[Path] = DATA_DIR / "config.toml"
CONFIG_FILE_PATH: Final[Path] = CONFIG_PATH
"""``~/.opscloud/config.toml`` — Main configuration file."""

GLOBAL_ENV_PATH: Final[Path] = DATA_DIR / ".env"
"""``~/.opscloud/.env`` — Global env vars / API keys."""

HOOKS_PATH: Final[Path] = DATA_DIR / "hooks.json"
"""``~/.opscloud/hooks.json`` — Lifecycle hooks."""

GLOBAL_MCP_PATH: Final[Path] = DATA_DIR / ".mcp.json"
"""``~/.opscloud/.mcp.json`` — Global MCP server definitions."""

CONVERSATION_HISTORY_DIR: Final[Path] = DATA_DIR / "conversation_history"
"""``~/.opscloud/conversation_history/`` — Offload conversation logs."""

LOGS_DIR: Final[Path] = DATA_DIR / "logs"

# ── Managed state files (.state/) ────────────────────────

AUTH_PATH: Final[Path] = STATE_DIR / "auth.json"
"""``~/.opscloud/.state/auth.json`` — Credential store."""

SESSIONS_DB_PATH: Final[Path] = STATE_DIR / "sessions.db"
"""``~/.opscloud/.state/sessions.db`` — SQLite conversation checkpoints."""

SERVER_DIR: Final[Path] = STATE_DIR / "server"
SERVER_RUNTIME_DIR: Final[Path] = SERVER_DIR

HISTORY_PATH: Final[Path] = STATE_DIR / "history.jsonl"
"""``~/.opscloud/.state/history.jsonl`` — Command input history."""

RECENT_MODELS_PATH: Final[Path] = STATE_DIR / "recent_models.json"
"""``~/.opscloud/.state/recent_models.json`` — Last /model switch."""

MCP_TRUST_PATH: Final[Path] = STATE_DIR / "mcp_trust.json"
"""``~/.opscloud/.state/mcp_trust.json`` — Saved MCP project approvals."""

SKILL_TRUST_PATH: Final[Path] = STATE_DIR / "skill_trust.json"
"""``~/.opscloud/.state/skill_trust.json`` — Skill trust decisions."""

ONBOARDING_MARKER: Final[Path] = STATE_DIR / "onboarding_complete"
"""``~/.opscloud/.state/onboarding_complete`` — First-run marker."""

# ── Plugin directory and state files ─────────────────────────

PLUGINS_DIR: Final[Path] = DATA_DIR / "plugins"
PLUGINS_CACHE_DIR: Final[Path] = PLUGINS_DIR / "cache"
PLUGIN_CACHE_DIR: Final[Path] = PLUGINS_CACHE_DIR
PLUGIN_DATA_DIR: Final[Path] = PLUGINS_DIR / "data"
PLUGIN_MARKETPLACES_DIR: Final[Path] = PLUGINS_DIR / "marketplaces"

PLUGIN_INSTALLED_PATH: Final[Path] = STATE_DIR / "installed_plugins.json"
PLUGIN_STATE_PATH: Final[Path] = STATE_DIR / "plugin_state.json"
PLUGIN_MARKETPLACES_PATH: Final[Path] = STATE_DIR / "plugin_marketplaces.json"

USER_SETTINGS_PATH: Final[Path] = DATA_DIR / "settings.json"


def project_opscloud_dir(project_root: Path) -> Path:
    """Return ``{project_root}/.opscloud`` — project-specific opscloud metadata dir."""
    return project_root / ".opscloud"


def project_settings_path(project_root: Path) -> Path:
    """Return ``{project_root}/.opscloud/settings.json`` — project-scope settings."""
    opscloud_settings = project_opscloud_dir(project_root) / "settings.json"
    if not opscloud_settings.exists() and (project_root / ".opscode" / "settings.json").exists():
        return project_root / ".opscode" / "settings.json"
    return opscloud_settings


def project_local_settings_path(project_root: Path) -> Path:
    """Return ``{project_root}/.opscloud/settings.local.json`` — local-scope settings."""
    opscloud_local = project_opscloud_dir(project_root) / "settings.local.json"
    if not opscloud_local.exists() and (project_root / ".opscode" / "settings.local.json").exists():
        return project_root / ".opscode" / "settings.local.json"
    return opscloud_local

# ── Project root markers ─────────────────────────────────

PROJECT_ROOT_MARKERS: Final[tuple[str, ...]] = (
    ".opscloud",
    ".git",
    "terragrunt.hcl",
    "main.tf",
    "cdk.json",
    "template.yaml",
    "template.yml",
    "samconfig.toml",
    "Pulumi.yaml",
    "Chart.yaml",
    "ansible.cfg",
    "pyproject.toml",
    "package.json",
    "Makefile",
)

DOTENV_DENIED_ENV_KEYS: Final[frozenset[str]] = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "DISPLAY",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONHOME",
        "NODE_PATH",
        "NODE_OPTIONS",
        "HISTFILE",
        "HISTSIZE",
        "SSH_AUTH_SOCK",
        "GPG_AGENT_INFO",
        "TMPDIR",
        "TEMP",
        "TMP",
    }
)

PROJECT_DOTENV_DENIED_ENV_KEYS: Final[frozenset[str]] = frozenset(
    {
        "OPSCLOUD_APPROVAL_MODE",
        "APPROVAL_MODE",
        "OPSCLOUD_SHELL_ALLOW_LIST",
        "SHELL_ALLOW_LIST",
        "OPSCLOUD_READ_ONLY",
        "READ_ONLY",
        "OPSCLOUD_CONFIG_DIR",
        "CONFIG_DIR",
        "OPSCLOUD_READ_PROJECT_DOTENV",
        "READ_PROJECT_DOTENV",
        "OPSCLOUD_SERVER_DB_PATH",
        "OPSCODE_APPROVAL_MODE",
        "OPSCODE_SHELL_ALLOW_LIST",
        "OPSCODE_READ_ONLY",
        "OPSCODE_CONFIG_DIR",
        "TERM_PROGRAM",
    }
)

RELOADABLE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "model_name",
        "model_provider",
        "reasoning_effort",
        "cloud_provider",
        "aws_region",
        "aws_profile",
        "shell_allow_list",
        "theme",
        "read_project_dotenv",
    }
)

AWS_PRESERVE_ENV_VARS: Final[frozenset[str]] = frozenset(
    {
        # AWS Core & Authentication
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_ROLE_ARN",
        "AWS_ROLE_SESSION_NAME",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_STS_REGIONAL_ENDPOINTS",
        "AWS_CA_BUNDLE",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN",
        "AWS_ENDPOINT_URL",
        "AWS_SDK_LOAD_CONFIG",
        "AWS_VAULT",
        "AWS_EC2_METADATA_DISABLED",
        "AWS_MAX_ATTEMPTS",
        "AWS_RETRY_MODE",
        "AWS_APP_NAME",
        "AWS_PAGER",
        # AWS CDK
        "CDK_DEFAULT_ACCOUNT",
        "CDK_DEFAULT_REGION",
        "CDK_DOCKER_DEFAULT_ACCOUNT",
        "CDK_CONTEXT_JSON",
        # AWS SAM
        "SAM_CLI_TELEMETRY",
        # Terraform & OpenTofu
        "TF_IN_AUTOMATION",
        "TF_DATA_DIR",
        "TF_PLUGIN_CACHE_DIR",
        "TF_CLI_CONFIG_FILE",
        # Pulumi
        "PULUMI_CONFIG_PASSPHRASE",
        "PULUMI_ACCESS_TOKEN",
        # Kubernetes / EKS
        "KUBECONFIG",
        "KUBE_NAMESPACE",
        "KUBE_CONTEXT",
        "KUBE_CLUSTER",
        "HELM_CONFIG_HOME",
        "HELM_CACHE_HOME",
        "HELM_DATA_HOME",
        # Docker / ECR
        "DOCKER_HOST",
        "DOCKER_CERT_PATH",
        "DOCKER_TLS_VERIFY",
    }
)

DEVOPS_PRESERVE_ENV_VARS = AWS_PRESERVE_ENV_VARS


# ── Agent / per-agent directory helpers ──────────────────

def agent_dir(name: str = DEFAULT_AGENT_NAME) -> Path:
    """Return agent directory.

    For the default agent ('opscloud'), returns ``~/.opscloud/`` directly.
    For custom named assistants, returns ``~/.opscloud/agents/{name}/``.
    """
    if not name or name == DEFAULT_AGENT_NAME:
        return DATA_DIR
    return DATA_DIR / "agents" / name


def user_skills_dir(name: str = DEFAULT_AGENT_NAME) -> Path:
    """Return ``~/.opscloud/skills/`` for default agent, or ``~/.opscloud/agents/{name}/skills/``."""
    if not name or name == DEFAULT_AGENT_NAME:
        return DATA_DIR / "skills"
    return agent_dir(name) / "skills"


def get_user_skills_dir(name: str = DEFAULT_AGENT_NAME) -> Path:
    """Return user skills directory."""
    return user_skills_dir(name)


def ensure_user_skills_dir(name: str = DEFAULT_AGENT_NAME) -> Path:
    """Create and return user skills directory."""
    d = user_skills_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_built_in_skills_dir() -> Path:
    """Return the directory containing built-in skills packaged with opscloud."""
    return Path(__file__).parent.parent / "built_in_skills"


def get_user_agent_skills_dir() -> Path | None:
    """Return the user's tool-agnostic ``~/.agents/skills`` directory."""
    return AGENTS_SHARED_DIR / "skills"


def get_user_claude_skills_dir() -> Path | None:
    """Return the user's experimental ``~/.claude/skills`` directory."""
    return USER_HOME / ".claude" / "skills"


def user_agents_dir(name: str = DEFAULT_AGENT_NAME) -> Path:
    """Return ``~/.opscloud/agents/`` for default agent, or ``~/.opscloud/agents/{name}/agents/``."""
    if not name or name == DEFAULT_AGENT_NAME:
        return DATA_DIR / "agents"
    return agent_dir(name) / "agents"


DEFAULT_AGENT_MD_TEMPLATE = """# OpsCloud Persistent Memory & Operational Context

## User Preferences & Defaults
<!-- opscloud:onboarding-name:start -->
- The user's role/name is "Cloud Platform & DevOps Engineer".
- Primary Cloud Provider: AWS
- Default Region: us-east-1
- IaC Tool Preference: Terraform / OpenTofu
- Deployment Strategy: Verification first with dry-run before mutation
<!-- opscloud:onboarding-name:end -->

## Operational Guardrails & Patterns
- **Dry-Run Validation**: Always execute non-destructive checks (`terraform plan`, `kubectl diff`, `--dry-run=server`) prior to applying infrastructure changes.
- **Context Hygiene**: Offload high-volume command output and log dumps to files rather than terminal outputs.
- **Zero-Trust Security**: Apply least-privilege IAM policies and avoid hardcoded credentials or wildcard permissions.
"""


def primary_agent_md() -> Path:
    """Return ``~/.opscloud/AGENTS.md``."""
    return DATA_DIR / "AGENTS.md"


def user_agent_md(name: str = DEFAULT_AGENT_NAME) -> Path:
    """Return ``~/.opscloud/AGENTS.md`` for default agent, or ``~/.opscloud/agents/{name}/AGENTS.md``."""
    if not name or name == DEFAULT_AGENT_NAME:
        return primary_agent_md()
    return agent_dir(name) / "AGENTS.md"


def ensure_user_agent_md(name: str = DEFAULT_AGENT_NAME) -> Path:
    """Ensure ``~/.opscloud/AGENTS.md`` exists on disk.

    Initializes it with default operational preferences and guardrails if empty or missing.
    For custom named assistants, also initializes the assistant-specific memory file.

    Returns:
        The primary persistent memory path (``~/.opscloud/AGENTS.md``).
    """
    ensure_data_dir()
    root_md = primary_agent_md()
    if not root_md.exists() or root_md.stat().st_size == 0:
        try:
            root_md.write_text(DEFAULT_AGENT_MD_TEMPLATE, encoding="utf-8")
        except OSError as e:
            logger.warning("Could not write primary memory file %s: %s", root_md, e)

    if name and name != DEFAULT_AGENT_NAME:
        d = ensure_agent_dir(name)
        agent_md = d / "AGENTS.md"
        if not agent_md.exists() or agent_md.stat().st_size == 0:
            try:
                agent_md.write_text(
                    f"# Assistant-Specific Memory: {name}\n\n"
                    "<!-- Assistant-specific operational learnings and notes -->\n",
                    encoding="utf-8",
                )
            except OSError as e:
                logger.warning("Could not write assistant memory file %s: %s", agent_md, e)
    return root_md


# ── Project directory helpers ────────────────────────────

def project_skills_dir(project_root: Path) -> Path:
    """Return ``{project_root}/.opscloud/skills/``."""
    return project_opscloud_dir(project_root) / "skills"


def get_project_skills_dir(project_root: Path | None) -> Path | None:
    """Return the project-level opscloud skills directory, when in a project."""
    return None if project_root is None else project_skills_dir(project_root)


def ensure_project_skills_dir(project_root: Path | None) -> Path | None:
    """Create and return the project-level opscloud skills directory."""
    d = get_project_skills_dir(project_root)
    if d is not None:
        d.mkdir(parents=True, exist_ok=True)
    return d


def get_project_agent_skills_dir(project_root: Path | None) -> Path | None:
    """Return the project's tool-agnostic ``.agents/skills`` directory."""
    return None if project_root is None else project_root / ".agents" / "skills"


def get_project_claude_skills_dir(project_root: Path | None) -> Path | None:
    """Return the project's experimental ``.claude/skills`` directory."""
    return None if project_root is None else project_root / ".claude" / "skills"


def display_path(path: Path | str) -> str:
    """Abbreviate a path for user display with leading ``~`` when under user home."""
    p = Path(path)
    try:
        rel = p.relative_to(USER_HOME)
        return str(Path("~") / rel)
    except (ValueError, RuntimeError):
        return str(path)


class _ProfilePathProxy:
    @staticmethod
    def agent_skills_dir(agent: str) -> Path:
        return user_skills_dir(agent)

    @property
    def config_file(self) -> Path:
        return CONFIG_PATH


class _PathsSnapshot:
    """Convenience snapshot mirroring dcode PATHS interface for display and paths."""

    profile = _ProfilePathProxy()

    @staticmethod
    def display(path: Path | str) -> str:
        return display_path(path)


PATHS = _PathsSnapshot()


def project_agents_dir(project_root: Path) -> Path:
    """Return ``{project_root}/.opscloud/agents/``."""
    return project_opscloud_dir(project_root) / "agents"


def project_mcp_paths(project_root: Path) -> list[Path]:
    """Return candidate MCP config paths for a project, in precedence order."""
    return [
        project_root / ".mcp.json",
        project_root / "mcp.json",
        project_opscloud_dir(project_root) / ".mcp.json",
        project_opscloud_dir(project_root) / "mcp.json",
    ]


def project_agent_md_paths(project_root: Path) -> list[Path]:
    """Return candidate AGENTS.md paths for a project."""
    return [
        project_opscloud_dir(project_root) / "AGENTS.md",
        project_root / "AGENTS.md",
    ]


def find_project_root(start_path: Path | str | None = None) -> Path | None:
    """Climb directories upwards to find project root by well-known marker."""
    if start_path is None:
        try:
            from opscloud.project_utils import get_server_project_context

            ctx = get_server_project_context()
            if ctx is not None and ctx.project_root is not None:
                return ctx.project_root
            if ctx is not None:
                start_path = ctx.user_cwd
        except Exception:
            pass

    try:
        curr = Path(start_path or Path.cwd()).resolve()
    except OSError:
        return None

    home = Path.home().resolve()
    for directory in [curr, *curr.parents]:
        if directory == home:
            continue
        for marker in PROJECT_ROOT_MARKERS:
            if (directory / marker).exists():
                return directory
    return None



# ── Ensure directories exist ────────────────────────────

def ensure_data_dir() -> Path:
    """Create ``~/.opscloud/`` and standard subdirectories if they don't exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "skills").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "agents").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "memory").mkdir(parents=True, exist_ok=True)
    return DATA_DIR


def ensure_state_dir() -> Path:
    """Create ``~/.opscloud/.state/`` if it doesn't exist."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR


def ensure_agent_dir(name: str = DEFAULT_AGENT_NAME) -> Path:
    """Create and return agent directory."""
    if not name or name == DEFAULT_AGENT_NAME:
        return ensure_data_dir()
    d = agent_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_plugins_dir() -> Path:
    """Create ``~/.opscloud/plugins/`` if it doesn't exist."""
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    return PLUGINS_DIR


def ensure_conversation_history_dir() -> Path:
    """Create ``~/.opscloud/conversation_history/`` if it doesn't exist, restricted to current user."""
    CONVERSATION_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    try:
        CONVERSATION_HISTORY_DIR.chmod(0o700)
    except OSError:
        pass
    return CONVERSATION_HISTORY_DIR


def upsert_env_vars(env_dict: dict[str, str], env_path: Path | None = None) -> bool:
    """Upsert key-value pairs into a .env file atomically."""
    import contextlib
    import tempfile

    target_path = env_path or GLOBAL_ENV_PATH
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        if target_path.exists():
            content = target_path.read_text(encoding="utf-8")
            lines = content.splitlines()

        new_lines: list[str] = []
        seen_keys: set[str] = set()

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                new_lines.append(line)
                continue

            raw_key = stripped.removeprefix("export ").split("=", 1)[0].strip()
            if not raw_key:
                new_lines.append(line)
                continue

            if raw_key in env_dict:
                if raw_key not in seen_keys:
                    new_lines.append(f"{raw_key}={env_dict[raw_key]}")
                    seen_keys.add(raw_key)
            else:
                if raw_key not in seen_keys:
                    new_lines.append(line)
                    seen_keys.add(raw_key)

        for k, v in env_dict.items():
            if k not in seen_keys:
                new_lines.append(f"{k}={v}")
                seen_keys.add(k)

        final_content = "\n".join(new_lines).strip() + "\n" if new_lines else ""

        fd, tmp_path = tempfile.mkstemp(dir=target_path.parent, suffix=".tmp")
        try:
            with open(fd, "w", encoding="utf-8") as f:
                f.write(final_content)
            Path(tmp_path).replace(target_path)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink()
            raise
        return True
    except Exception as exc:
        logger.exception("Failed to upsert env vars in %s: %s", target_path, exc)
        return False


# ── Path classification (for doctor/diagnostics) ────────

_MISSING_ERRNOS = {errno.ENOENT, errno.ENOTDIR}


class PathState(StrEnum):
    """Whether a probed path exists, is absent, or could not be read."""

    EXISTS = "exists"
    """The path is present on disk."""

    MISSING = "missing"
    """The path is absent (and its parents are readable)."""

    UNREADABLE = "unreadable"
    """Existence could not be determined because ``Path.stat()`` raised."""


def classify_path(path: Path) -> PathState:
    """Classify a path as existing, missing, or unreadable."""
    try:
        path.stat()
    except OSError as exc:
        if exc.errno in _MISSING_ERRNOS:
            return PathState.MISSING
        logger.debug("Could not stat %s", path, exc_info=True)
        return PathState.UNREADABLE
    else:
        return PathState.EXISTS
