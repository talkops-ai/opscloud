"""Main CLI entry point and argument parser for OpsCloud.

Routes between interactive TUI mode (default), subcommands (doctor, auth,
threads, mcp, plugin, skill), and non-interactive headless mode
(-p / -n / positional prompt / piped stdin).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, overload

from opscloud._version import __version__
from opscloud.approval_mode import ApprovalMode
from opscloud.cli.commands import (
    run_auth_command,
    run_doctor_command,
    run_mcp_command,
    run_threads_command,
    setup_mcp_parser,
)
from opscloud.cli.interactive import run_interactive
from opscloud.cli.non_interactive import run_non_interactive
from opscloud.output import OutputFormat, add_json_output_arg
from opscloud.state.goal_state_limits import validate_rubric
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)


# ── Argument Types ───────────────────────────────────────


def positive_int(value: str) -> int:
    """Validate positive integer arguments (> 0)."""
    try:
        ivalue = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a valid integer") from exc
    if ivalue <= 0:
        raise argparse.ArgumentTypeError(f"{value!r} must be greater than 0")
    return ivalue


def non_negative_int(value: str) -> int:
    """Validate non-negative integer arguments (>= 0)."""
    try:
        ivalue = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a valid integer") from exc
    if ivalue < 0:
        raise argparse.ArgumentTypeError(f"{value!r} must be non-negative")
    return ivalue


def shell_allow_list_arg(value: str) -> list[str]:
    """Parse comma-separated list of allowed shell commands."""
    from opscloud.config.settings import parse_shell_allow_list

    parsed = parse_shell_allow_list(value)
    return parsed or []


# ── Rubric Resolution ────────────────────────────────────


@overload
def _resolve_rubric_text(rubric: None) -> None: ...


@overload
def _resolve_rubric_text(rubric: str) -> str: ...


def _resolve_rubric_text(rubric: str | None) -> str | None:
    """Resolve the rubric from `--rubric` into one string.

    `--rubric` accepts literal text, or `@path` to read a file. File paths
    may be absolute, relative to the process working directory, or `~`-expanded.
    """
    if rubric is None:
        return None

    if rubric.startswith("@"):
        path = rubric[1:]
        try:
            text = Path(path).expanduser().read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            msg = f"Could not read rubric file {path!r}: {exc}."
            raise ValueError(msg) from exc
        if not text.strip():
            msg = f"Rubric file {path!r} is empty."
            raise ValueError(msg)
        resolved = text.strip()
        validate_rubric(resolved)
        return resolved

    if not rubric.strip():
        msg = "--rubric must not be empty."
        raise ValueError(msg)
    resolved = rubric.strip()
    validate_rubric(resolved)
    return resolved


# ── Stdin detection & piping ─────────────────────────────


def _apply_stdin_pipe(args: argparse.Namespace) -> None:
    """Read piped stdin and merge it into the parsed CLI arguments."""
    explicit_stdin = getattr(args, "stdin", False)
    if sys.stdin is None:
        if explicit_stdin:
            print("Error: --stdin was passed but stdin is not available.", file=sys.stderr)
            sys.exit(1)
        return

    try:
        is_tty = sys.stdin.isatty()
    except (ValueError, OSError):
        if explicit_stdin:
            print("Error: --stdin was passed but stdin state could not be determined.", file=sys.stderr)
            sys.exit(1)
        return

    if is_tty:
        if explicit_stdin:
            print(
                "Error: --stdin was passed but stdin is a terminal. Pipe input or pass a prompt directly.",
                file=sys.stderr,
            )
            sys.exit(1)
        return

    max_stdin_bytes = 10 * 1024 * 1024  # 10 MiB
    try:
        stdin_text = sys.stdin.read(max_stdin_bytes + 1)
    except UnicodeDecodeError:
        print("Error: Could not read piped input — ensure the input is valid text", file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError) as exc:
        if not explicit_stdin or "pytest" in str(exc).lower():
            return
        print(f"Error: Failed to read piped input: {exc}", file=sys.stderr)
        sys.exit(1)

    if len(stdin_text) > max_stdin_bytes:
        print(f"Error: Piped input exceeds {max_stdin_bytes // (1024 * 1024)} MiB limit.", file=sys.stderr)
        sys.exit(1)

    stdin_text = stdin_text.strip()
    if not stdin_text:
        return

    if getattr(args, "prompt", None):
        args.prompt = f"{stdin_text}\n\n{args.prompt}"
    elif getattr(args, "initial_prompt", None) is not None:
        if args.initial_prompt:
            args.initial_prompt = f"{stdin_text}\n\n{args.initial_prompt}"
        else:
            args.initial_prompt = stdin_text
    elif getattr(args, "initial_skill", None) and not explicit_stdin:
        args.initial_prompt = stdin_text
    else:
        args.prompt = stdin_text

    # Restore terminal fd for interactive mode if possible
    try:
        tty_fd = os.open("/dev/tty", os.O_RDONLY)
        os.dup2(tty_fd, 0)
        os.close(tty_fd)
    except OSError:
        pass


# ── Approval Mode Resolution ─────────────────────────────


def _resolve_approval_mode(args: argparse.Namespace) -> ApprovalMode:
    """Resolve flags into typed ApprovalMode."""
    if getattr(args, "smart", False):
        return ApprovalMode.SMART
    if getattr(args, "auto_approve", False):
        return ApprovalMode.AUTO
    raw = getattr(args, "approval_mode", None)
    if raw:
        if raw.lower() in ("smart", "s", "jev"):
            return ApprovalMode.SMART
        if raw.lower() in ("always", "manual"):
            return ApprovalMode.MANUAL
        if raw.lower() in ("auto", "a"):
            return ApprovalMode.AUTO

    # Non-interactive / headless execution (-p / --prompt) defaults to AUTO for autonomous execution,
    # while interactive TUI defaults to MANUAL for human-in-the-loop safety.
    if getattr(args, "prompt", None):
        return ApprovalMode.AUTO
    return ApprovalMode.MANUAL


# ── Parser Builder ───────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Build the comprehensive OpsCloud command-line argument parser."""
    parser = argparse.ArgumentParser(
        prog="opscloud",
        description="OpsCloud — Autonomous Cloud Operations & DevOps Coding Agent.",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    # Prompt & Headless flags
    parser.add_argument(
        "-p",
        "--prompt",
        "-n",
        "--non-interactive",
        dest="prompt",
        type=str,
        default=None,
        help="Run non-interactively (headless) with the given prompt.",
    )

    # Interactive Initial Prompts & Seeding
    parser.add_argument(
        "-m",
        "--message",
        "--initial-prompt",
        dest="initial_prompt",
        type=str,
        default=None,
        help="Initial prompt to auto-submit when interactive session starts.",
    )
    parser.add_argument(
        "-s",
        "--skill",
        dest="initial_skill",
        type=str,
        default=None,
        help="Invoke a skill when interactive or headless session starts.",
    )
    parser.add_argument(
        "--startup-cmd",
        dest="startup_cmd",
        type=str,
        default=None,
        help="Shell command executed at startup before the first prompt.",
    )

    # Output formatting
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        default=False,
        help="Clean output for piping stdout — only agent's final text goes to stdout. Requires headless mode.",
    )
    parser.add_argument(
        "--no-stream",
        dest="no_stream",
        action="store_true",
        default=False,
        help="Buffer response and write to stdout at once instead of streaming chunks. Requires headless mode.",
    )
    add_json_output_arg(parser, default="text")

    # CI/CD Execution Limits
    parser.add_argument(
        "--max-turns",
        dest="max_turns",
        type=positive_int,
        default=None,
        help="Maximum agentic turns before stopping (>= 1). Requires headless mode.",
    )
    parser.add_argument(
        "--timeout",
        dest="timeout",
        type=positive_int,
        default=None,
        help="Hard wall-clock timeout in seconds (>= 1). Exits 124 on expiry. Requires headless mode.",
    )
    parser.add_argument(
        "--recursion-limit",
        dest="recursion_limit",
        type=positive_int,
        default=None,
        help="Override main agent's LangGraph recursion_limit (graph step budget).",
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        default=False,
        help="Read input from stdin explicitly.",
    )

    # Cloud & Environment Overrides
    parser.add_argument(
        "--read-only",
        "--dry-run",
        dest="read_only",
        action="store_true",
        default=False,
        help="Enforce read-only cloud operations safety mode (blocks all mutating AWS/K8s/Terraform calls).",
    )
    parser.add_argument(
        "--aws-profile",
        dest="aws_profile",
        type=str,
        default=None,
        help="AWS profile override for this session (e.g. staging, prod).",
    )
    parser.add_argument(
        "--aws-region",
        dest="aws_region",
        type=str,
        default=None,
        help="AWS region override for this session (e.g. us-east-1, eu-west-1).",
    )
    parser.add_argument(
        "-S",
        "--shell-allow-list",
        dest="shell_allow_list",
        type=shell_allow_list_arg,
        default=None,
        help="Comma-separated list of allowed shell commands ('recommended', 'all', or comma-separated names).",
    )

    # Model & Config
    parser.add_argument(
        "-M",
        "--model",
        dest="model",
        type=str,
        default=None,
        help="Model specification to use (e.g. anthropic:claude-3-5-sonnet, openai:gpt-4o, bedrock:anthropic.claude-3-5-sonnet).",
    )
    parser.add_argument(
        "--model-params",
        dest="model_params",
        type=str,
        default=None,
        help="Extra kwargs to pass to model as JSON string (e.g. '{\"temperature\": 0.0}').",
    )
    parser.add_argument(
        "--profile-override",
        dest="profile_override",
        type=str,
        default=None,
        help="Override model profile fields as JSON string.",
    )
    parser.add_argument(
        "--default-model",
        dest="default_model",
        nargs="?",
        const="__SHOW__",
        default=None,
        help="Set or show the default model for future launches.",
    )
    parser.add_argument(
        "--clear-default-model",
        dest="clear_default_model",
        action="store_true",
        default=False,
        help="Clear configured default model.",
    )

    # Approvals
    approval_group = parser.add_mutually_exclusive_group()
    approval_group.add_argument(
        "--approval-mode",
        dest="approval_mode",
        type=str,
        choices=["manual", "auto", "smart"],
        default=None,
        help="HITL tool approval mode: 'manual', 'auto', or 'smart' (TypeSafe AI Jev).",
    )
    approval_group.add_argument(
        "-y",
        "--auto-approve",
        dest="auto_approve",
        action="store_true",
        default=False,
        help="Enable classic classifier-backed Auto mode.",
    )
    approval_group.add_argument(
        "--smart",
        dest="smart",
        action="store_true",
        default=False,
        help="Enable Jev-powered Smart approval mode (<100ms System One safety gate).",
    )

    # MCP
    parser.add_argument(
        "--mcp-config",
        dest="mcp_config",
        type=str,
        default=None,
        help="Path to MCP servers JSON configuration file.",
    )
    parser.add_argument(
        "--no-mcp",
        dest="no_mcp",
        action="store_true",
        default=False,
        help="Disable all MCP tool loading.",
    )
    parser.add_argument(
        "--trust-project-mcp",
        dest="trust_project_mcp",
        action="store_true",
        default=False,
        help="Skip interactive confirmation prompts for project-level MCP servers.",
    )

    # Session & State
    parser.add_argument(
        "-r",
        "--resume",
        "--resume-thread",
        dest="resume_thread",
        type=str,
        default=None,
        help="Resume an existing session thread ID.",
    )
    parser.add_argument(
        "--cwd",
        dest="cwd",
        type=str,
        default=None,
        help="Working directory path for this execution.",
    )
    parser.add_argument(
        "--goal",
        type=str,
        default=None,
        help="Initial goal objective to establish (for interactive TUI mode).",
    )
    parser.add_argument(
        "--rubric",
        type=str,
        default=None,
        help="Acceptance criteria to grade against (literal text or @path); used with headless mode.",
    )
    parser.add_argument(
        "--rubric-model",
        type=str,
        default=None,
        help="Model specification for the rubric grader agent.",
    )
    parser.add_argument(
        "--rubric-max-iterations",
        type=positive_int,
        default=None,
        help="Maximum evaluation iterations for the rubric grader.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Enable verbose logging.",
    )
    parser.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error", "critical"],
        type=str.lower,
        default=None,
        help="Set logging level (default: info, or debug with -v).",
    )
    parser.add_argument(
        "--log-dir",
        type=str,
        default=None,
        help="Directory for log files (default: /tmp/opscloud_logs).",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=None,
        help="Log file path or filename (default: opscloud.log in log directory).",
    )

    # Subcommands
    subparsers = parser.add_subparsers(dest="subcommand", help="Operational subcommands")
    doctor_parser = subparsers.add_parser("doctor", help="Run system & cloud environment health checks.")
    add_json_output_arg(doctor_parser)

    auth_parser = subparsers.add_parser("auth", help="Show AWS STS authentication status.")
    add_json_output_arg(auth_parser)

    threads_parser = subparsers.add_parser("threads", help="Manage session threads.")
    add_json_output_arg(threads_parser)
    threads_sub = threads_parser.add_subparsers(dest="threads_command")
    threads_list = threads_sub.add_parser("list", aliases=["ls"], help="List saved session threads.")
    add_json_output_arg(threads_list)
    threads_del = threads_sub.add_parser("delete", help="Delete a session thread.")
    threads_del.add_argument("thread_id", help="Thread ID to delete.")
    add_json_output_arg(threads_del)

    setup_mcp_parser(subparsers)

    from opscloud.plugins.commands_cli import setup_plugin_parser

    setup_plugin_parser(subparsers)

    from opscloud.skills import setup_skills_parser

    setup_skills_parser(subparsers)

    return parser


# ── Argument Validation ──────────────────────────────────


def _validate_args(args: argparse.Namespace) -> int | None:
    """Validate argument compatibility and mutual exclusions."""
    # Goal and rubric flag validations
    if getattr(args, "goal", None) and getattr(args, "rubric", None):
        print(
            "Error: Cannot specify both --goal and --rubric. Use --goal for interactive sessions, or --rubric with -p for headless verification.",
            file=sys.stderr,
        )
        return 1

    if getattr(args, "goal", None) and getattr(args, "prompt", None):
        print(
            "Error: Cannot specify both --goal and --rubric/--prompt. Use --goal for interactive sessions, or --rubric with -p for headless verification.",
            file=sys.stderr,
        )
        return 1

    if getattr(args, "rubric", None) and not getattr(args, "prompt", None):
        print("Error: --rubric requires -p/--prompt for headless verification.", file=sys.stderr)
        return 1

    if getattr(args, "rubric_model", None) and not getattr(args, "rubric", None):
        print("Error: --rubric-model requires --rubric.", file=sys.stderr)
        return 1

    if getattr(args, "rubric_max_iterations", None) and not getattr(args, "rubric", None):
        print("Error: --rubric-max-iterations requires --rubric.", file=sys.stderr)
        return 1

    if (getattr(args, "quiet", False) or getattr(args, "no_stream", False)) and not getattr(args, "prompt", None):
        flags = []
        if getattr(args, "quiet", False):
            flags.append("--quiet")
        if getattr(args, "no_stream", False):
            flags.append("--no-stream")
        flag = " and ".join(flags)
        print(
            f"Error: {flag} requires -p/--prompt, -n, positional prompt, or piped stdin.",
            file=sys.stderr,
        )
        return 2

    if getattr(args, "max_turns", None) is not None and not getattr(args, "prompt", None):
        print(
            "Error: --max-turns requires -p/--prompt, -n, positional prompt, or piped stdin.",
            file=sys.stderr,
        )
        return 2

    if getattr(args, "timeout", None) is not None and not getattr(args, "prompt", None):
        print(
            "Error: --timeout requires -p/--prompt, -n, positional prompt, or piped stdin.",
            file=sys.stderr,
        )
        return 2

    if getattr(args, "no_mcp", False) and getattr(args, "mcp_config", None):
        print("Error: --no-mcp and --mcp-config are mutually exclusive.", file=sys.stderr)
        return 2

    return None


# ── Main Entrypoint ──────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """Main CLI entrypoint function."""
    raw_args = list(sys.argv[1:]) if argv is None else list(argv)

    # 1. Fast-path for --version
    if len(raw_args) == 1 and raw_args[0] in {"-v", "--version"}:
        print(f"opscloud {__version__}")
        return 0

    parser = build_parser()

    known_subcommands = {
        "doctor",
        "auth",
        "threads",
        "mcp",
        "plugin",
        "plugins",
        "skill",
        "skills",
        "help",
    }

    # Hoist global logging flags to the front so they work seamlessly
    # across both root invocations and subcommands (e.g. opscloud doctor --verbose)
    hoisted_args: list[str] = []
    remaining_args: list[str] = []
    it = iter(raw_args)
    for arg in it:
        if arg in {"-v", "--verbose"}:
            hoisted_args.append(arg)
        elif arg.startswith(("--log-level=", "--log-dir=", "--log-file=")):
            hoisted_args.append(arg)
        elif arg in {"--log-level", "--log-dir", "--log-file"}:
            hoisted_args.append(arg)
            val = next(it, None)
            if val is not None:
                hoisted_args.append(val)
        else:
            remaining_args.append(arg)
    reordered_args = hoisted_args + remaining_args

    # Identify flags with value arguments vs flags without
    flags_with_values: set[str] = {
        opt for act in parser._actions if act.nargs not in (0, "?") for opt in act.option_strings
    }
    optional_val_flags: set[str] = {
        opt for act in parser._actions if act.nargs == "?" for opt in act.option_strings
    }

    # Find the first positional token to check if it's a subcommand or a prompt
    first_pos = None
    scan_it = iter(reordered_args)
    for token in scan_it:
        if token in flags_with_values:
            next(scan_it, None)
        elif token in optional_val_flags:
            pass
        elif not token.startswith("-"):
            first_pos = token
            break

    is_subcommand = first_pos in known_subcommands
    if is_subcommand:
        args = parser.parse_args(reordered_args)
    else:
        # Preprocess positional prompt if -p/--prompt not already present
        has_prompt_flag = any(a in {"-p", "--prompt", "-n", "--non-interactive"} for a in reordered_args)
        if first_pos is not None and not has_prompt_flag:
            processed_args: list[str] = []
            prompt_injected = False
            build_it = iter(reordered_args)
            for token in build_it:
                if token in flags_with_values:
                    processed_args.append(token)
                    val = next(build_it, None)
                    if val is not None:
                        processed_args.append(val)
                elif token in optional_val_flags:
                    processed_args.append(token)
                elif not token.startswith("-") and token == first_pos and not prompt_injected:
                    processed_args.extend(["-p", token])
                    prompt_injected = True
                else:
                    processed_args.append(token)
            args = parser.parse_args(processed_args)
        else:
            args = parser.parse_args(reordered_args)

    # 2. Configure system logging based on CLI flags and environment
    from opscloud.utils.logger import configure_logging

    log_level = "DEBUG" if getattr(args, "verbose", False) else getattr(args, "log_level", None)
    log_to_console = (
        True
        if getattr(args, "verbose", False)
        else (False if getattr(args, "json_output", False) or getattr(args, "quiet", False) else None)
    )

    configure_logging(
        level=log_level,
        log_to_console=log_to_console,
        log_dir=getattr(args, "log_dir", None),
        log_file=getattr(args, "log_file", None),
    )

    # 3. Ingest piped stdin
    _apply_stdin_pipe(args)

    # 4. Handle subcommands
    raw_fmt = getattr(args, "output_format", "text")
    output_format: OutputFormat = "json" if raw_fmt == "json" else "text"

    if args.subcommand == "doctor":
        return run_doctor_command(output_format)
    elif args.subcommand == "auth":
        return run_auth_command(output_format)
    elif args.subcommand == "threads":
        subcmd = getattr(args, "threads_command", None)
        if subcmd == "delete":
            # Delete thread command
            import asyncio
            from opscloud.state.session import delete_thread

            try:
                deleted = asyncio.run(delete_thread(args.thread_id))
                if output_format == "json":
                    from opscloud.output import write_json

                    write_json("threads delete", {"thread_id": args.thread_id, "deleted": deleted})
                else:
                    if deleted:
                        print(f"Deleted thread: {args.thread_id}")
                    else:
                        print(f"Thread not found: {args.thread_id}", file=sys.stderr)
                        return 1
                return 0
            except Exception as e:
                print(f"Error deleting thread {args.thread_id}: {e}", file=sys.stderr)
                return 1
        return run_threads_command(output_format)
    elif args.subcommand == "mcp":
        return run_mcp_command(args)
    elif args.subcommand in {"plugin", "plugins"}:
        from opscloud.plugins.commands_cli import execute_plugin_command

        try:
            execute_plugin_command(args)
            return 0
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 1
    elif args.subcommand in {"skill", "skills"}:
        from opscloud.skills import execute_skills_command

        try:
            execute_skills_command(args)
            return 0
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 1

    # 4. Handle default model configuration
    if getattr(args, "clear_default_model", False):
        from opscloud.model.config import clear_default_model

        if clear_default_model():
            print("Default model cleared.")
            return 0
        else:
            print("Error: Could not clear default model.", file=sys.stderr)
            return 1

    if getattr(args, "default_model", None) is not None:
        from opscloud.model.config import load_default_model, save_default_model
        from opscloud.model.factory import normalize_model_spec

        if args.default_model == "__SHOW__":
            curr = load_default_model()
            if curr:
                print(f"Default model: {curr}")
            else:
                print("No default model set.")
            return 0

        norm = normalize_model_spec(args.default_model)
        if save_default_model(norm):
            print(f"Default model set to {norm}")
            return 0
        else:
            print("Error: Could not save default model.", file=sys.stderr)
            return 1

    # 5. Parse model kwargs and profile JSON if provided
    model_params: dict[str, Any] | None = None
    if getattr(args, "model_params", None):
        try:
            model_params = json.loads(args.model_params)
            if not isinstance(model_params, dict):
                print("Error: --model-params must be a JSON object.", file=sys.stderr)
                return 1
        except json.JSONDecodeError as exc:
            print(f"Error: --model-params is not valid JSON: {exc}", file=sys.stderr)
            return 1

    profile_override: dict[str, Any] | None = None
    if getattr(args, "profile_override", None):
        try:
            profile_override = json.loads(args.profile_override)
            if not isinstance(profile_override, dict):
                print("Error: --profile-override must be a JSON object.", file=sys.stderr)
                return 1
        except json.JSONDecodeError as exc:
            print(f"Error: --profile-override is not valid JSON: {exc}", file=sys.stderr)
            return 1

    # 6. Validate argument combinations
    val_err = _validate_args(args)
    if val_err is not None:
        return val_err

    # 7. Resolve Rubric text if provided
    resolved_rubric = None
    if args.rubric:
        try:
            resolved_rubric = _resolve_rubric_text(args.rubric)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    # 8. Resolve Approval Mode
    approval_mode = _resolve_approval_mode(args)

    # 9. Headless / Non-interactive execution mode
    if args.prompt:
        model = args.model
        if not model:
            try:
                from opscloud.model.factory import _get_default_model_spec

                model = _get_default_model_spec()
            except Exception as e:
                print(f"Error: {e}", file=sys.stderr)
                return 1

        json_out = getattr(args, "output_format", "text") == "json"

        from opscloud.project_utils import ProjectContext

        project_context = (
            ProjectContext.from_user_cwd(Path(args.cwd))
            if args.cwd
            else ProjectContext.from_user_cwd(Path.cwd())
        )
        effective_cwd = str(project_context.user_cwd)
        return run_non_interactive(
            args.prompt,
            model=model,
            model_params=model_params,
            profile_override=profile_override,
            approval_mode=approval_mode,
            shell_allow_list=args.shell_allow_list,
            read_only=args.read_only,
            aws_profile=args.aws_profile,
            aws_region=args.aws_region,
            quiet=args.quiet,
            no_stream=args.no_stream,
            json_output=json_out,
            max_turns=args.max_turns,
            timeout=float(args.timeout) if args.timeout is not None else None,
            rubric=resolved_rubric,
            rubric_model=args.rubric_model,
            rubric_max_iterations=args.rubric_max_iterations,
            recursion_limit=args.recursion_limit,
            initial_skill=args.initial_skill,
            startup_cmd=args.startup_cmd,
            mcp_config_path=args.mcp_config,
            no_mcp=args.no_mcp,
            trust_project_mcp=args.trust_project_mcp,
            thread_id=args.resume_thread,
            cwd=effective_cwd,
        )

    # 10. Interactive TUI mode
    from opscloud.project_utils import ProjectContext

    project_context = (
        ProjectContext.from_user_cwd(Path(args.cwd))
        if args.cwd
        else ProjectContext.from_user_cwd(Path.cwd())
    )
    effective_cwd = str(project_context.user_cwd)
    return run_interactive(
        model=args.model,
        approval_mode=approval_mode,
        resume_thread=args.resume_thread,
        cwd=effective_cwd,
        goal=args.goal,
        initial_prompt=args.initial_prompt,
        initial_skill=args.initial_skill,
        startup_cmd=args.startup_cmd,
        read_only=args.read_only,
        aws_profile=args.aws_profile,
        aws_region=args.aws_region,
        shell_allow_list=args.shell_allow_list,
        mcp_config_path=args.mcp_config,
        no_mcp=args.no_mcp,
        trust_project_mcp=args.trust_project_mcp,
    )


cli_main = main

if __name__ == "__main__":
    sys.exit(main())
