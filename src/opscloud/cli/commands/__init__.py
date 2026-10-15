"""CLI subcommands."""

from opscloud.cli.commands.auth import run_auth_command
from opscloud.cli.commands.doctor import run_doctor_command
from opscloud.cli.commands.mcp import run_mcp_command, setup_mcp_parser
from opscloud.cli.commands.serve import run_serve_command, setup_serve_parser
from opscloud.cli.commands.threads import run_threads_command

__all__ = [
    "run_auth_command",
    "run_doctor_command",
    "run_mcp_command",
    "run_serve_command",
    "run_threads_command",
    "setup_mcp_parser",
    "setup_serve_parser",
]

