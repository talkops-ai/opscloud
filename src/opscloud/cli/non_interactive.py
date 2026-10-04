"""Non-interactive (headless CLI) runner for OpsCloud.

Connects to the OpsCloud server session, streams agent output to stdout,
supports --quiet for clean Unix piping, --no-stream for buffered loggers,
--json for CI machine-readable envelopes, and enforces safety caps (--max-turns, --timeout).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import uuid
from typing import Any

from opscloud.approval_mode import ApprovalMode
from opscloud.cli.server_manager import server_session
from opscloud.config.settings import sync_aws_env_aliases
from opscloud.output import write_json
from opscloud.server import ServerConfig
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)


async def _run_headless(
    prompt: str,
    *,
    model: str | None = None,
    model_params: dict[str, Any] | None = None,
    profile_override: dict[str, Any] | None = None,
    assistant_id: str = "opscloud",
    approval_mode: ApprovalMode | str = ApprovalMode.AUTO,
    shell_allow_list: list[str] | None = None,
    read_only: bool = False,
    aws_profile: str | None = None,
    aws_region: str | None = None,
    quiet: bool = False,
    no_stream: bool = False,
    json_output: bool = False,
    max_turns: int | None = None,
    timeout: float | None = None,
    rubric: str | None = None,
    rubric_model: str | None = None,
    rubric_max_iterations: int | None = None,
    recursion_limit: int | None = None,
    initial_skill: str | None = None,
    startup_cmd: str | None = None,
    mcp_config_path: str | None = None,
    no_mcp: bool = False,
    trust_project_mcp: bool = False,
    thread_id: str | None = None,
    cwd: str | None = None,
) -> int:
    """Execute a single headless run against the server session."""
    # 1. Startup command
    if startup_cmd:
        try:
            if not quiet and not json_output:
                print(f"[Running startup command: {startup_cmd}]", file=sys.stderr)
            proc = subprocess.run(startup_cmd, shell=True, capture_output=True, text=True)
            if proc.stdout and not quiet and not json_output:
                print(proc.stdout.strip(), file=sys.stderr)
            if proc.returncode != 0 and not quiet and not json_output:
                print(f"[Startup command exited with code: {proc.returncode}]", file=sys.stderr)
        except Exception as exc:
            if not quiet and not json_output:
                print(f"[Startup command failed: {exc}]", file=sys.stderr)

    # 2. AWS Profile and Region environment synchronization
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

    # 3. Model spec resolution
    resolved_model = model
    if not resolved_model:
        try:
            from opscloud.model.factory import _get_default_model_spec

            resolved_model = _get_default_model_spec()
        except Exception:
            resolved_model = "anthropic:claude-3-5-sonnet-latest"

    # 4. Server Configuration
    from pathlib import Path
    from opscloud.project_utils import ProjectContext

    project_context = (
        ProjectContext.from_user_cwd(Path(cwd))
        if cwd is not None
        else ProjectContext.from_user_cwd(Path.cwd())
    )
    config = ServerConfig.from_cli_args(
        project_context=project_context,
        model=resolved_model,
        assistant_id=assistant_id,
        interactive=False,
        approval_mode=str(approval_mode),
        shell_allow_list=tuple(shell_allow_list) if shell_allow_list else None,
        read_only=read_only,
        aws_profile=aws_profile,
        aws_region=aws_region,
        mcp_config_path=mcp_config_path,
        no_mcp=no_mcp,
        trust_project_mcp=trust_project_mcp,
    )

    if thread_id:
        try:
            uuid.UUID(thread_id)
            effective_thread = thread_id
        except ValueError:
            # Deterministically convert custom thread identifier to a valid UUIDv5
            effective_thread = str(uuid.uuid5(uuid.NAMESPACE_DNS, thread_id))
    else:
        from opscloud.state.session import generate_thread_id

        effective_thread = generate_thread_id()

    # Route session logs to thread-specific log file
    from opscloud.utils.logger import bind_logging_to_thread

    bind_logging_to_thread(effective_thread)

    final_prompt = prompt
    if initial_skill:
        final_prompt = f"Use the skill '{initial_skill}' for this task:\n{prompt}"

    start_time = time.monotonic()
    turn_count = 0
    full_response: list[str] = []
    rubric_status: str | None = None

    logger.info(
        "Starting headless run | thread_id=%s model=%s prompt=%r",
        effective_thread,
        resolved_model or "default",
        final_prompt[:80] + ("..." if len(final_prompt) > 80 else ""),
    )

    with server_session(config) as agent:
        if not quiet and not json_output:
            print(f"OpsCloud running headless: {final_prompt}\n", file=sys.stderr)
            if rubric:
                print(f"Active Acceptance Criteria:\n{rubric}\n", file=sys.stderr)

        from opscloud.security.approval_mode import approval_mode_key, awrite_approval_mode
        try:
            await awrite_approval_mode(agent, effective_thread, mode=approval_mode)
        except Exception:
            logger.debug("Failed to pre-write approval mode to store", exc_info=True)

        context: dict[str, Any] = {
            "model": resolved_model,
            "approval_mode": str(approval_mode),
            "approval_mode_key": approval_mode_key(effective_thread),
            "thread_id": effective_thread,
            "read_only": read_only,
            "smart": str(approval_mode).lower() in {"smart", "jev"},
        }
        if model_params:
            context["model_params"] = model_params
        if profile_override:
            context["profile_overrides"] = profile_override
        if rubric_model:
            context["rubric_model"] = rubric_model
        if rubric_max_iterations is not None:
            context["rubric_max_iterations"] = rubric_max_iterations
        if shell_allow_list:
            context["shell_allow_list"] = shell_allow_list

        run_config: dict[str, Any] = {
            "configurable": {"thread_id": effective_thread},
        }
        if recursion_limit is not None:
            run_config["recursion_limit"] = recursion_limit

        input_data: dict[str, Any] = {"messages": [{"role": "user", "content": final_prompt}]}
        if rubric:
            input_data["rubric"] = rubric

        try:
            async for ns, mode, chunk in agent.astream(
                input_data,
                stream_mode=["messages", "updates", "custom"],
                subgraphs=True,
                config=run_config,
                context=context,
            ):
                if mode == "messages":
                    msg = chunk[0] if isinstance(chunk, tuple) else chunk
                    if isinstance(msg, str):
                        content = msg
                    elif isinstance(msg, dict):
                        content = msg.get("content", "")
                    else:
                        content = getattr(msg, "content", "")

                    text_piece = ""
                    if isinstance(content, str):
                        text_piece = content
                    elif isinstance(content, list):
                        parts: list[str] = []
                        for part in content:
                            if isinstance(part, str):
                                parts.append(part)
                            elif isinstance(part, dict) and part.get("type") == "text":
                                parts.append(part.get("text", ""))
                        text_piece = "".join(parts)

                    if text_piece:
                        full_response.append(text_piece)
                        if not no_stream and not json_output:
                            sys.stdout.write(text_piece)
                            sys.stdout.flush()

                elif mode == "updates":
                    turn_count += 1
                    logger.debug("Turn %d completed | thread_id=%s", turn_count, effective_thread)
                    if max_turns is not None and turn_count > max_turns:
                        logger.warning("Reached max-turns limit (%d) | thread_id=%s", max_turns, effective_thread)
                        if not quiet and not json_output:
                            print(f"\n[Reached max-turns limit ({max_turns})]", file=sys.stderr)
                        return 124

                    if isinstance(chunk, dict):
                        messages = chunk.get("messages", [])
                        for m in messages:
                            name = getattr(m, "name", "")
                            if name:
                                logger.info(
                                    "Turn %d: Tool executed: %s | thread_id=%s",
                                    turn_count,
                                    name,
                                    effective_thread,
                                )
                                if not quiet and not json_output:
                                    print(f"\n[Tool: {name}]", file=sys.stderr)

                elif mode == "custom":
                    if isinstance(chunk, dict):
                        event_type = chunk.get("type") or chunk.get("event")
                        if event_type == "subagent":
                            phase = chunk.get("phase")
                            sub_type = chunk.get("subagent_type") or chunk.get("id") or "subagent"
                            label = chunk.get("label") or chunk.get("description") or ""
                            logger.info(
                                "Subagent %s (%s): %s | thread_id=%s",
                                sub_type,
                                phase,
                                label,
                                effective_thread,
                            )
                            if not quiet and not json_output:
                                msg = f": {label}" if label else ""
                                if phase == "start":
                                    print(f"\n[Subagent started: {sub_type}{msg}]", file=sys.stderr)
                                elif phase == "complete":
                                    dur = chunk.get("duration_ms")
                                    dur_str = f" in {dur}ms" if dur else ""
                                    print(f"\n[Subagent completed: {sub_type}{dur_str}]", file=sys.stderr)
                                elif phase == "error":
                                    err = chunk.get("error") or "Unknown error"
                                    print(f"\n[Subagent error: {sub_type} - {err}]", file=sys.stderr)
                        elif event_type == "rubric_evaluation_start":
                            it = chunk.get("iteration", 0)
                            logger.info("Acceptance criteria grading started (attempt %d) | thread_id=%s", it + 1, effective_thread)
                            if not quiet and not json_output:
                                print(f"\n[Grading acceptance criteria (attempt {it + 1})...]", file=sys.stderr)
                        elif event_type == "rubric_evaluation_end":
                            verdict = chunk.get("verdict") or chunk.get("result") or "unknown"
                            rubric_status = verdict
                            logger.info("Acceptance criteria check completed: verdict=%s | thread_id=%s", verdict, effective_thread)
                            if not quiet and not json_output:
                                if verdict == "satisfied":
                                    print("\n[Acceptance criteria satisfied!]", file=sys.stderr)
                                else:
                                    print(f"\n[Acceptance criteria check: {verdict}]", file=sys.stderr)
                                    expl = chunk.get("explanation") or chunk.get("feedback")
                                    if expl:
                                        print(f"[Deficiencies]\n{expl}", file=sys.stderr)

            # Flush buffered text if --no-stream was requested
            if no_stream and not json_output and full_response:
                sys.stdout.write("".join(full_response))
                sys.stdout.flush()

            duration = round(time.monotonic() - start_time, 2)
            logger.info(
                "Headless run completed in %.2fs (%d turns) | thread_id=%s status=%s",
                duration,
                turn_count,
                effective_thread,
                rubric_status or "success",
            )

            if not quiet and not json_output:
                sys.stdout.write("\n")
                sys.stdout.flush()

            # Output standardized JSON envelope if --json was requested
            if json_output:
                status = "success"
                if rubric and rubric_status != "satisfied":
                    status = "criteria_unmet"
                write_json(
                    "run",
                    {
                        "status": status,
                        "thread_id": effective_thread,
                        "turns": turn_count,
                        "duration_seconds": round(time.monotonic() - start_time, 2),
                        "rubric_status": rubric_status,
                        "content": "".join(full_response),
                    },
                )

            if rubric and rubric_status != "satisfied":
                logger.warning("Headless run completed but criteria were not satisfied: %s", rubric_status)
                return 2

            return 0

        except Exception as e:
            logger.error("Headless run failed: %s", e, exc_info=True)
            if not quiet and not json_output:
                print(f"\nHeadless run error: {e}", file=sys.stderr)
            return 1


def run_non_interactive(
    prompt: str,
    *,
    model: str | None = None,
    model_params: dict[str, Any] | None = None,
    profile_override: dict[str, Any] | None = None,
    assistant_id: str = "opscloud",
    approval_mode: ApprovalMode | str = ApprovalMode.AUTO,
    shell_allow_list: list[str] | None = None,
    read_only: bool = False,
    aws_profile: str | None = None,
    aws_region: str | None = None,
    quiet: bool = False,
    no_stream: bool = False,
    json_output: bool = False,
    max_turns: int | None = None,
    timeout: float | None = None,
    rubric: str | None = None,
    rubric_model: str | None = None,
    rubric_max_iterations: int | None = None,
    recursion_limit: int | None = None,
    initial_skill: str | None = None,
    startup_cmd: str | None = None,
    mcp_config_path: str | None = None,
    no_mcp: bool = False,
    trust_project_mcp: bool = False,
    thread_id: str | None = None,
    cwd: str | None = None,
) -> int:
    """Synchronous entrypoint for running OpsCloud non-interactively with timeout and signals."""
    coro = _run_headless(
        prompt,
        model=model,
        model_params=model_params,
        profile_override=profile_override,
        assistant_id=assistant_id,
        approval_mode=approval_mode,
        shell_allow_list=shell_allow_list,
        read_only=read_only,
        aws_profile=aws_profile,
        aws_region=aws_region,
        quiet=quiet,
        no_stream=no_stream,
        json_output=json_output,
        max_turns=max_turns,
        timeout=timeout,
        rubric=rubric,
        rubric_model=rubric_model,
        rubric_max_iterations=rubric_max_iterations,
        recursion_limit=recursion_limit,
        initial_skill=initial_skill,
        startup_cmd=startup_cmd,
        mcp_config_path=mcp_config_path,
        no_mcp=no_mcp,
        trust_project_mcp=trust_project_mcp,
        thread_id=thread_id,
        cwd=cwd,
    )
    if timeout is not None:
        coro = asyncio.wait_for(coro, timeout=timeout)

    try:
        return asyncio.run(coro)
    except TimeoutError:
        if not quiet and not json_output:
            print(f"\nError: agent timed out after {timeout}s.", file=sys.stderr)
        return 124
    except KeyboardInterrupt:
        if not quiet and not json_output:
            print("\n[Interrupted]", file=sys.stderr)
        return 130
    except Exception as exc:
        logger.exception("Non-interactive execution failed: %s", exc)
        if not quiet and not json_output:
            print(f"\nError: {exc}", file=sys.stderr)
        return 1
