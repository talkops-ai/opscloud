"""Threads management CLI command."""

from __future__ import annotations

import asyncio
from rich.console import Console
from rich.table import Table

from opscloud.output import OutputFormat, write_json
from opscloud.state.session import (
    format_relative_timestamp,
    format_timestamp,
    list_threads,
)


def run_threads_command(output_format: OutputFormat = "text") -> int:
    threads = asyncio.run(list_threads(include_message_count=True))

    if output_format == "json":
        write_json("threads list", threads)
        return 0

    console = Console()
    console.print("\n[bold yellow]Saved Session Threads[/bold yellow]\n")
    if not threads:
        console.print("[dim]No saved sessions found in SQLite database.[/dim]\n")
        return 0

    table = Table(border_style="cyan", show_lines=False)
    table.add_column("Thread ID", style="bold green", no_wrap=True)
    table.add_column("Cloud / Env", style="bold magenta", no_wrap=True)
    table.add_column("Msgs", justify="right", style="cyan", no_wrap=True)
    table.add_column("Initial Prompt", style="white")
    table.add_column("Updated", style="dim", no_wrap=True)

    for t in threads:
        tid = t.get("thread_id", "")
        short_id = tid[:8] if len(tid) > 8 else tid
        cloud = t.get("cloud_badge") or t.get("cloud_provider") or "-"
        msg_count = str(t.get("message_count", 0))
        prompt = t.get("initial_prompt") or "(No initial prompt)"
        if len(prompt) > 50:
            prompt = prompt[:47] + "..."
        updated = format_relative_timestamp(t.get("updated_at")) or format_timestamp(t.get("updated_at"))

        table.add_row(short_id, cloud, msg_count, prompt, updated)

    console.print(table)
    console.print("\n[dim]To resume a thread, run:[/dim] opscloud -r <thread_id>\n")
    return 0
