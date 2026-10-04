"""Auth management CLI command."""

from __future__ import annotations

from rich.console import Console

from opscloud.config.aws import get_aws_context
from opscloud.output import OutputFormat, write_json


def run_auth_command(output_format: OutputFormat = "text") -> int:
    ctx = get_aws_context()

    if output_format == "json":
        write_json(
            "auth",
            {
                "authenticated": ctx.is_authenticated,
                "account_id": ctx.account_id,
                "arn": ctx.arn,
                "user_id": ctx.user_id,
                "region": ctx.region,
                "profile": ctx.profile,
                "error": ctx.error,
            },
        )
        return 0 if ctx.is_authenticated else 1

    console = Console()
    console.print("\n[bold yellow]AWS STS Authentication Status[/bold yellow]\n")
    if ctx.is_authenticated:
        console.print("[bold green]Authenticated Successfully[/bold green]")
        console.print(f"  Account ID: {ctx.account_id}")
        console.print(f"  Identity ARN: {ctx.arn}")
        console.print(f"  User ID: {ctx.user_id}")
        console.print(f"  Region: {ctx.region}")
        console.print(f"  Profile: {ctx.profile}\n")
        return 0
    else:
        console.print("[bold red]Not Authenticated[/bold red]")
        console.print(f"  Error: {ctx.error or 'No credentials found'}")
        console.print(
            "  Tip: Configure AWS credentials via `aws configure` or export AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY\n"
        )
        return 1
