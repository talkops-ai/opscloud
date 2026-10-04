"""Global constant definitions for opscloud."""

from pathlib import Path

DEFAULT_AGENT_NAME = "opscloud"
DEFAULT_ASSISTANT_ID = "opscloud"

# App directories
USER_HOME = Path.home()
OPSCLOUD_HOME = USER_HOME / ".opscloud"
STATE_DIR = OPSCLOUD_HOME / ".state"
SESSIONS_DB_NAME = "sessions.db"
SESSIONS_DB_PATH = STATE_DIR / SESSIONS_DB_NAME
SERVER_RUNTIME_DIR = STATE_DIR / "server"
CONFIG_FILE_PATH = OPSCLOUD_HOME / "config.toml"
PLUGINS_CACHE_DIR = OPSCLOUD_HOME / "plugins" / "cache"

# Environment variables prefixes
ENV_PREFIX = "OPSCLOUD_"
SERVER_ENV_PREFIX = "OPSCLOUD_SERVER_"

# Tooling and shell execution
SHELL_TIMEOUT_SECONDS = 60
DEFAULT_PORT = 0  # Ephemeral

# Large tool results prefix (used by rubrics and compaction)
LARGE_TOOL_RESULTS_PREFIX = "/large_tool_results/"
SYSTEM_MESSAGE_PREFIX = "[System Message]"

FS_TOOL_NAMES = frozenset(
    {"ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep", "execute", "run_command"}
)

READONLY_FS_TOOLS = frozenset(
    {
        "ls",
        "read_file",
        "glob",
        "grep",
        "view_file",
        "list_dir",
        "dir_list",
        "grep_search",
        "file_search",
        "read_url_content",
        "fetch_web_page",
        "search_web",
        "get_goal",
        "get_rubric",
        "update_goal",
        "write_todos",
    }
)
