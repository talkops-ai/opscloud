"""Interactive TUI runner for OpsCloud."""

from __future__ import annotations

import os
import subprocess
import sys

from opscloud.approval_mode import ApprovalMode
from opscloud.config.settings import sync_aws_env_aliases
from opscloud.server import ServerConfig, ServerProcess
from opscloud.ui.app import OpsCloudApp
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)


def run_interactive(
    *,
    model: str | None = None,
    approval_mode: ApprovalMode | str = ApprovalMode.MANUAL,
    resume_thread: str | None = None,
    cwd: str | None = None,
    goal: str | None = None,
    initial_prompt: str | None = None,
    initial_skill: str | None = None,
    startup_cmd: str | None = None,
    read_only: bool = False,
    aws_profile: str | None = None,
    aws_region: str | None = None,
    shell_allow_list: list[str] | None = None,
    mcp_config_path: str | None = None,
    no_mcp: bool = False,
    trust_project_mcp: bool = False,
) -> int:
    """Launch interactive TUI session with background server and cloud context."""
    if resume_thread:
        from opscloud.utils.logger import bind_logging_to_thread

        bind_logging_to_thread(resume_thread)

    # 1. Startup command
    if startup_cmd:
        try:
            print(f"[Running startup command: {startup_cmd}]", file=sys.stderr)
            subprocess.run(startup_cmd, shell=True, check=False)
        except Exception as e:
            logger.warning("Startup command failed: %s", e)

    # 2. AWS Profile & Region sync
    from opscloud.config.cloud_profiles import get_active_aws_profile, get_active_aws_region

    if not aws_profile:
        aws_profile = get_active_aws_profile()

    if not aws_region:
        aws_region = get_active_aws_region()

    if aws_profile:
        os.environ["AWS_PROFILE"] = aws_profile
    if aws_region:
        os.environ["AWS_REGION"] = aws_region
    if aws_profile or aws_region:
        sync_aws_env_aliases()

    from pathlib import Path
    from opscloud.project_utils import ProjectContext

    project_context = (
        ProjectContext.from_user_cwd(Path(cwd))
        if cwd is not None
        else ProjectContext.from_user_cwd(Path.cwd())
    )
    resolved_cwd = str(project_context.user_cwd)

    defer_server_start = False
    resolved_model = model
    if not resolved_model:
        try:
            from opscloud.model.factory import _get_default_model_spec

            resolved_model = _get_default_model_spec()
        except Exception:
            resolved_model = None
            defer_server_start = True

    proc: ServerProcess | None = None
    url: str | None = None
    if not defer_server_start and resolved_model:
        config = ServerConfig.from_cli_args(
            project_context=project_context,
            model=resolved_model,
            interactive=True,
            approval_mode=str(approval_mode),
            read_only=read_only,
            aws_profile=aws_profile,
            aws_region=aws_region,
            shell_allow_list=tuple(shell_allow_list) if shell_allow_list else None,
            mcp_config_path=mcp_config_path,
            no_mcp=no_mcp,
            trust_project_mcp=trust_project_mcp,
        )
        proc = ServerProcess(config)
        url = proc.start()

    effective_prompt = initial_prompt
    if initial_skill and not effective_prompt:
        effective_prompt = f"Use skill {initial_skill}"

    try:
        app = OpsCloudApp(
            server_url=url,
            server=proc,
            server_proc=proc,
            thread_id=resume_thread,
            model=resolved_model,
            approval_mode=approval_mode,
            defer_server_start=defer_server_start,
            goal=goal,
            cwd=resolved_cwd,
        )
        if effective_prompt and hasattr(app, "_initial_prompt"):
            app._initial_prompt = effective_prompt

        thread_id = app.run()
        if thread_id and isinstance(thread_id, str) and thread_id != "local_session":
            try:
                from rich.console import Console
                from rich.text import Text

                console = Console()
                console.print()
                console.print("[dim]Resume this thread with:[/dim]")
                hint = Text("opscloud -r ", style="cyan")
                hint.append(thread_id, style="cyan")
                console.print(hint)
            except Exception:
                pass
        return 0
    except Exception as e:
        logger.critical("OpsCloud TUI run failed: %s", e, exc_info=True)
        print(f"Error starting OpsCloud: {e}")
        return 1
    finally:
        if proc is not None:
            proc.stop()
