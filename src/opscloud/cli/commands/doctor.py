"""Doctor command verifying AWS credentials, tools, and environment."""

from __future__ import annotations

import shutil
import sys

from rich.console import Console
from rich.table import Table

from opscloud.config.aws import get_aws_context
from opscloud.output import OutputFormat, write_json


def run_doctor_command(output_format: OutputFormat = "text") -> int:
    aws_ctx = get_aws_context()
    tools = ["aws", "kubectl", "terraform", "helm", "docker", "git"]
    tool_statuses: dict[str, str | None] = {}
    for tool_name in tools:
        tool_statuses[tool_name] = shutil.which(tool_name)

    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

    from opscloud.utils.logger import (
        get_active_log_dir,
        get_active_log_file,
        get_configured_log_level,
        is_console_logging_enabled,
    )

    log_dir = str(get_active_log_dir())
    log_file = str(get_active_log_file())
    log_level = get_configured_log_level()
    console_logging = is_console_logging_enabled()

    if output_format == "json":
        write_json(
            "doctor",
            {
                "aws_sts": {
                    "authenticated": aws_ctx.is_authenticated,
                    "account_id": aws_ctx.account_id,
                    "region": aws_ctx.region,
                    "profile": aws_ctx.profile,
                    "error": aws_ctx.error,
                },
                "tools": tool_statuses,
                "python": py_ver,
                "logging": {
                    "directory": log_dir,
                    "file": log_file,
                    "level": log_level,
                    "console_enabled": console_logging,
                },
            },
        )
        return 0

    console = Console()
    console.print("\n[bold yellow]OpsCloud System & Cloud Environment Doctor[/bold yellow]\n")

    table = Table(title="Environment Diagnostics", border_style="cyan")
    table.add_column("Component", style="bold")
    table.add_column("Status")
    table.add_column("Details")

    # 1. AWS Credentials
    if aws_ctx.is_authenticated:
        table.add_row(
            "AWS STS Identity",
            "[green]OK[/green]",
            f"Account: {aws_ctx.account_id}, Region: {aws_ctx.region}, Profile: {aws_ctx.profile}",
        )
    else:
        table.add_row(
            "AWS STS Identity",
            "[yellow]Unauthenticated[/yellow]",
            aws_ctx.error or "No active AWS credentials found via STS",
        )

    # 2. Tooling
    for tool_name, path in tool_statuses.items():
        if path:
            table.add_row(f"CLI: {tool_name}", "[green]Found[/green]", path)
        else:
            table.add_row(f"CLI: {tool_name}", "[dim]Not installed[/dim]", "Optional")

    # 3. Python version
    table.add_row("Python Runtime", "[green]OK[/green]", f"Python {py_ver}")

    # 4. Logging subsystem
    table.add_row(
        "Log Destination",
        "[green]Active[/green]",
        f"{log_file} (level={log_level}, console={'enabled' if console_logging else 'silent'})",
    )

    console.print(table)
    console.print()
    return 0
