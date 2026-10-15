"""Central webhook server CLI command (`opscloud serve` / `opscloud webhooks`)."""

from __future__ import annotations

import argparse
import contextlib
import os
from pathlib import Path
import sys
import time
from typing import Any, TextIO

from colorama import Fore, Style
from rich.console import Console
from rich.table import Table

from opscloud.config.settings import get_settings
from opscloud.output import add_json_output_arg, write_json
from opscloud.project_utils import ProjectContext
from opscloud.server import ServerConfig, ServerProcess
from opscloud.utils.logger import configure_logging
from opscloud.webhooks.models import WebhookConfig
from opscloud.webhooks.providers import create_default_registry

DEFAULT_WEBHOOK_PORT = 2024
DEFAULT_WEBHOOK_HOST = "0.0.0.0"

_LEVEL_BADGE_COLORS: dict[str, str] = {
    "[DEBUG]": f"{Fore.LIGHTBLACK_EX}[DEBUG]{Style.RESET_ALL}",
    "[INFO]": f"{Fore.BLUE}[INFO]{Style.RESET_ALL}",
    "[WARNING]": f"{Fore.YELLOW}[WARNING]{Style.RESET_ALL}",
    "[ERROR]": f"{Fore.RED}[ERROR]{Style.RESET_ALL}",
    "[CRITICAL]": f"{Fore.LIGHTRED_EX}[CRITICAL]{Style.RESET_ALL}",
}


def _colorize_log_line(line: str, *, use_color: bool = True) -> str:
    """Apply ANSI color to leading [LEVEL] tags for live console streaming."""
    if not use_color:
        return line
    for tag, colored in _LEVEL_BADGE_COLORS.items():
        if line.startswith(tag):
            return colored + line[len(tag) :]
    return line


def _drain_live_logs(
    log_reader: TextIO | None,
    *,
    stream: TextIO | None = None,
    quiet: bool = False,
) -> None:
    """Read and print any newly appended lines from the server log file."""
    if log_reader is None or quiet:
        return
    out = stream or sys.stdout
    use_color = bool(getattr(out, "isatty", lambda: False)())
    with contextlib.suppress(Exception):
        while True:
            line = log_reader.readline()
            if not line:
                break
            out.write(_colorize_log_line(line, use_color=use_color))
        out.flush()


def setup_serve_parser(subparsers: Any) -> None:
    """Register `opscloud serve` (and `webhooks` alias) subcommand parser."""
    serve_parser = subparsers.add_parser(
        "serve",
        aliases=["webhook", "webhooks"],
        help="Start OpsCloud in central webhook server mode (SNS, PagerDuty, Alertmanager, CloudEvents, Jenkins, GitHub).",
    )
    serve_parser.add_argument(
        "action",
        nargs="?",
        default="start",
        choices=["start", "list", "ls"],
        help="Action to perform: 'start' (default) launches the webhook server; 'list' prints supported webhook endpoints.",
    )
    serve_parser.add_argument(
        "--host",
        type=str,
        default=None,
        help=f"Host interface to bind (default: configured webhooks.host or {DEFAULT_WEBHOOK_HOST}).",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"Port to listen on (default: configured webhooks.port or {DEFAULT_WEBHOOK_PORT}).",
    )
    serve_parser.add_argument(
        "--list",
        action="store_true",
        dest="list_only",
        help="List all supported webhook providers and exit without starting the server.",
    )
    add_json_output_arg(serve_parser)
    serve_parser.add_argument(
        "--output-format",
        dest="output_format",
        choices=["text", "json"],
        default=argparse.SUPPRESS,
        help="Output format for provider listing.",
    )
    serve_parser.add_argument(
        "--approval-mode",
        dest="approval_mode",
        type=str,
        choices=["manual", "smart", "auto", "yolo"],
        default=argparse.SUPPRESS,
        help="Tool approval mode for webhook runs: 'smart' (default Jev gate), 'auto' (LLM classifier), 'yolo' (unrestricted), or 'manual'.",
    )
    serve_parser.add_argument(
        "-y",
        "--auto-approve",
        dest="auto_approve",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Enable classifier-backed Auto tool approval mode.",
    )
    serve_parser.add_argument(
        "--smart",
        dest="smart",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Enable Jev-powered Smart approval mode (<100ms System One safety gate).",
    )
    serve_parser.add_argument(
        "--yolo",
        dest="yolo",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Enable unrestricted YOLO mode for webhook runs without classifier or human gating.",
    )
    serve_parser.add_argument(
        "--read-only",
        "--dry-run",
        dest="read_only",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Enforce read-only cloud operations safety mode (blocks mutating AWS/K8s/Terraform calls).",
    )
    serve_parser.add_argument(
        "-M",
        "--model",
        dest="model",
        type=str,
        default=argparse.SUPPRESS,
        help="Model specification to use (e.g. anthropic:claude-sonnet-4-5, openai:gpt-4o).",
    )
    serve_parser.add_argument(
        "--aws-profile",
        dest="aws_profile",
        type=str,
        default=argparse.SUPPRESS,
        help="AWS profile override for webhook runs.",
    )
    serve_parser.add_argument(
        "--aws-region",
        dest="aws_region",
        type=str,
        default=argparse.SUPPRESS,
        help="AWS region override for webhook runs.",
    )
    serve_parser.add_argument(
        "--cwd",
        dest="cwd",
        type=str,
        default=argparse.SUPPRESS,
        help="Working directory path for the webhook server.",
    )
    serve_parser.add_argument(
        "--mcp-config",
        dest="mcp_config",
        type=str,
        default=argparse.SUPPRESS,
        help="Path to MCP servers JSON configuration file.",
    )
    serve_parser.add_argument(
        "--no-mcp",
        dest="no_mcp",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Disable all MCP tool loading.",
    )
    serve_parser.add_argument(
        "--trust-project-mcp",
        dest="trust_project_mcp",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Trust project-level MCP servers in unattended webhook mode.",
    )
    serve_parser.add_argument(
        "--log-level",
        dest="log_level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        type=str.upper,
        default=argparse.SUPPRESS,
        help="Server log level for webhook operations (default: INFO).",
    )
    serve_parser.add_argument(
        "-V",
        "--verbose",
        dest="verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Enable verbose DEBUG logging for webhook operations.",
    )
    serve_parser.add_argument(
        "-q",
        "--quiet",
        dest="quiet",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Suppress live log streaming to stdout while the webhook server runs.",
    )


def _render_providers_table(
    console: Console,
    base_url: str,
    webhook_cfg: WebhookConfig,
) -> None:
    registry = create_default_registry()
    table = Table(
        title=f"OpsCloud Central Webhook Endpoints ({base_url})",
        border_style="cyan",
        show_header=True,
        header_style="bold magenta",
    )
    table.add_column("Provider", style="bold cyan")
    table.add_column("Webhook Endpoint URL", style="green")
    table.add_column("Aliases", style="dim")
    table.add_column("Auth Status", justify="center")
    table.add_column("Supported Integrations")

    for item in registry.list_providers():
        provider = item["provider"]
        secret = webhook_cfg.get_provider_secret(provider)
        auth_badge = "[green]HMAC/Token[/green]" if secret else "[yellow]Open (VPC)[/yellow]"
        aliases_str = ", ".join(item["aliases"]) if item["aliases"] else "-"
        table.add_row(
            provider,
            f"{base_url}{item['endpoint']}",
            aliases_str,
            auth_badge,
            item["description"],
        )

    console.print(table)


def _resolve_serve_approval_mode(args: argparse.Namespace, settings: Any) -> str:
    """Resolve approval mode for `opscloud serve` (supports manual, smart, auto, yolo)."""
    raw_approval = getattr(args, "approval_mode", None)
    if getattr(args, "yolo", False) or (raw_approval and str(raw_approval).strip().lower() in {"yolo", "y"}):
        return "yolo"
    if getattr(args, "auto_approve", False) or (raw_approval and str(raw_approval).strip().lower() in {"auto", "a"}):
        return "auto"
    if getattr(args, "smart", False) or (raw_approval and str(raw_approval).strip().lower() in {"smart", "s", "jev"}):
        return "smart"
    if raw_approval and str(raw_approval).strip().lower() in {"manual", "m", "always"}:
        return "manual"

    cfg_mode = str(getattr(settings, "approval_mode", "") or "").strip().lower()
    if cfg_mode in {"yolo", "auto", "smart"}:
        return cfg_mode

    # Unattended central webhook mode defaults to 'smart' (Jev safety gate)
    return "smart"


def _resolve_serve_log_level(args: argparse.Namespace) -> str:
    """Resolve log level for `opscloud serve` (defaults to INFO)."""
    if getattr(args, "verbose", False):
        return "DEBUG"
    raw_level = getattr(args, "log_level", None)
    if raw_level and str(raw_level).strip().upper() in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        return str(raw_level).strip().upper()
    return "INFO"


def run_serve_command(args: argparse.Namespace) -> int:
    """Execute the `opscloud serve` / `opscloud webhooks` command."""
    console = Console()
    settings = get_settings()
    action = getattr(args, "action", "start")
    list_only = getattr(args, "list_only", False) or action in {"list", "ls"}
    host = getattr(args, "host", None) or settings.webhook_host or DEFAULT_WEBHOOK_HOST
    raw_port = getattr(args, "port", None)
    port = int(raw_port if raw_port is not None else (settings.webhook_port or DEFAULT_WEBHOOK_PORT))
    output_format = getattr(args, "output_format", "text")

    webhook_cfg = WebhookConfig.from_settings(settings)
    registry = create_default_registry()
    display_host = "127.0.0.1" if host == "0.0.0.0" else host
    base_url = f"http://{display_host}:{port}"

    if list_only:
        providers = registry.list_providers()
        if output_format == "json":
            write_json(
                "serve list",
                {
                    "base_url": base_url,
                    "providers": [
                        {
                            **p,
                            "url": f"{base_url}{p['endpoint']}",
                            "secret_configured": bool(webhook_cfg.get_provider_secret(p["provider"])),
                        }
                        for p in providers
                    ],
                },
            )
            return 0
        console.print()
        _render_providers_table(console, base_url, webhook_cfg)
        console.print()
        return 0

    cwd = getattr(args, "cwd", None)
    project_context = (
        ProjectContext.from_user_cwd(Path(cwd))
        if cwd
        else ProjectContext.from_user_cwd(Path.cwd())
    )

    model = getattr(args, "model", None) or settings.model_name
    if not model:
        try:
            from opscloud.model.factory import _get_default_model_spec

            model = _get_default_model_spec()
        except Exception:
            model = None

    effective_approval = _resolve_serve_approval_mode(args, settings)
    read_only = bool(getattr(args, "read_only", False))
    server_log_level = _resolve_serve_log_level(args)
    quiet = bool(getattr(args, "quiet", False))

    # Ensure active logging level is propagated to child server subprocess via environment
    configure_logging(level=server_log_level)

    server_config = ServerConfig.from_cli_args(
        project_context=project_context,
        model=model,
        interactive=False,
        auto_approve=(effective_approval == "auto"),
        approval_mode=effective_approval,
        read_only=read_only,
        aws_profile=getattr(args, "aws_profile", None) or settings.aws_profile,
        aws_region=getattr(args, "aws_region", None) or settings.aws_region,
        shell_allow_list=getattr(args, "shell_allow_list", None) or settings.shell_allow_list,
        mcp_config_path=getattr(args, "mcp_config", None),
        no_mcp=bool(getattr(args, "no_mcp", False)),
        trust_project_mcp=bool(getattr(args, "trust_project_mcp", False)),
        server_log_level=server_log_level,
    )

    proc = ServerProcess(server_config, host=host, port=port)
    console.print(
        f"\n[bold cyan]Starting OpsCloud Central Webhook Server[/bold cyan] "
        f"(host=[bold]{host}[/bold], port=[bold]{proc.port}[/bold], "
        f"approval_mode=[bold]{effective_approval}[/bold], read_only=[bold]{read_only}[/bold], "
        f"log_level=[bold]{server_log_level}[/bold])...\n"
    )

    log_reader: TextIO | None = None
    try:
        url = proc.start()
        _render_providers_table(console, url, webhook_cfg)
        console.print(
            f"\n[bold green]● Ready![/bold green] Listening for webhooks at [bold]{url}/webhooks/{{provider}}[/bold] "
            "(Press Ctrl+C to stop)"
        )
        if isinstance(getattr(proc, "log_file", None), Path):
            console.print(
                f"[dim]  Log file: {proc.log_file} (streaming live webhook & agent logs below)[/dim]\n"
            )
            with contextlib.suppress(Exception):
                proc.log_file.parent.mkdir(parents=True, exist_ok=True)
                proc.log_file.touch(exist_ok=True)
                log_reader = open(proc.log_file, encoding="utf-8", errors="replace")
                log_reader.seek(0, os.SEEK_END)
        else:
            console.print()

        while proc.process is not None and proc.process.poll() is None:
            _drain_live_logs(log_reader, quiet=quiet)
            time.sleep(0.15)
        _drain_live_logs(log_reader, quiet=quiet)
        return proc.process.returncode if proc.process and proc.process.returncode is not None else 0
    except KeyboardInterrupt:
        _drain_live_logs(log_reader, quiet=quiet)
        console.print("\n[yellow]Shutting down OpsCloud Webhook Server...[/yellow]")
        return 0
    finally:
        if log_reader is not None:
            with contextlib.suppress(Exception):
                log_reader.close()
        proc.stop()
