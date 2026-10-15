"""Prompt expansion pipeline for custom commands and skills.

Handles argument sanitization and substitution, environment variable expansion,
inline shell snippet execution, `@file` mention extraction, XML command header
formatting, space-to-colon namespace resolution, and stacked prompt chaining.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from opscloud.commands.custom.models import (
    CommandPermissionContext,
    CustomCommandDefinition,
    ExpandedCommandPrompt,
    ShellType,
)
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

_ESCAPED_DOLLAR_SENTINEL = "\x00ESCAPED_DOLLAR\x00"
_LEADING_BANG_RE = re.compile(r"(^|\s)!")
_INDEXED_ARGUMENTS_RE = re.compile(r"\$ARGUMENTS\[(\d+)\]")
_POSITIONAL_DOLLAR_RE = re.compile(r"\$(\d+)(?!\w)")
_FENCED_SHELL_BLOCK_RE = re.compile(r"^```!\s*\n?([\s\S]*?)\n?^```\s*$", re.MULTILINE)
_INLINE_SHELL_RE = re.compile(r"(?<!\\)!`([^`]+)`")
_REGULAR_FENCED_CODE_RE = re.compile(r"^```(?!\!)[^\n]*\n[\s\S]*?^```\s*$", re.MULTILINE)
_REGULAR_INLINE_CODE_RE = re.compile(r"(?<!!)`[^`\n]+`")
_FILE_MENTION_RE = re.compile(r"(?:^|(?<=\s))@([A-Za-z0-9_./~-]+\.[A-Za-z0-9_-]+|[A-Za-z0-9_./~-]+/[A-Za-z0-9_./~-]+)")

MAX_STACKED_PROMPT_COMMANDS = 5


def sanitize_command_arguments(raw_args: str) -> str:
    """Neutralize `!` shell-execution patterns inside user-supplied arguments.

    Prevents argument values from injecting inline ``!`cmd` `` or ```` ```! ````
    shell blocks when substituted into the template prior to shell execution.
    """
    if not raw_args:
        return ""
    sanitized = raw_args.replace("`!", "` !").replace("!`", "! `")
    sanitized = _LEADING_BANG_RE.sub(r"\1\\!", sanitized)
    return sanitized


def parse_positional_arguments(args_str: str) -> list[str]:
    """Tokenize an argument string with single/double quote and backslash awareness."""
    tokens: list[str] = []
    if not args_str or not args_str.strip():
        return tokens

    current: list[str] = []
    quote_char: str | None = None
    i = 0
    length = len(args_str)

    while i < length:
        ch = args_str[i]
        if ch == "\\" and i + 1 < length and args_str[i + 1] in ('"', "'", "\\"):
            current.append(args_str[i + 1])
            i += 2
            continue
        if quote_char is not None:
            if ch == quote_char:
                quote_char = None
            else:
                current.append(ch)
        elif ch in ('"', "'"):
            quote_char = ch
        elif ch in (" ", "\t", "\n"):
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(ch)
        i += 1

    if current:
        tokens.append("".join(current))
    return tokens


def substitute_arguments(
    template: str,
    raw_args: str | None,
    argument_names: list[str] | None = None,
    *,
    auto_append: bool = True,
) -> str:
    """Substitute `$ARGUMENTS`, `$ARGUMENTS[n]`, `$n`, and named `$arg` placeholders.

    1. Protects escaped `\\$` with a sentinel.
    2. Sanitizes `raw_args` via `sanitize_command_arguments`.
    3. Replaces named `$argName` (longest name first), `$ARGUMENTS[n]`, `$n`, and `$ARGUMENTS`.
    4. If no placeholder was present in `template` and `raw_args` is non-empty,
       appends `\\n\\nARGUMENTS: <sanitized_args>` when `auto_append=True`.
    5. Restores `\\$` -> `$`.
    """
    working = template.replace(r"\$", _ESCAPED_DOLLAR_SENTINEL)
    if not raw_args:
        return working.replace(_ESCAPED_DOLLAR_SENTINEL, "$")

    sanitized_args = sanitize_command_arguments(raw_args)
    positional = parse_positional_arguments(sanitized_args)

    before_sub = working

    if argument_names:
        indexed_names = sorted(
            enumerate(argument_names),
            key=lambda pair: len(pair[1]),
            reverse=True,
        )
        for idx, arg_name in indexed_names:
            escaped_name = re.escape(arg_name)
            pattern = re.compile(rf"\${escaped_name}(?![a-zA-Z0-9_])")
            replacement_val = positional[idx] if idx < len(positional) else ""
            working = pattern.sub(lambda _, val=replacement_val: val, working)

    def _replace_indexed(match: re.Match[str]) -> str:
        idx = int(match.group(1))
        return positional[idx] if 0 <= idx < len(positional) else ""

    working = _INDEXED_ARGUMENTS_RE.sub(_replace_indexed, working)
    working = _POSITIONAL_DOLLAR_RE.sub(_replace_indexed, working)
    working = working.replace("$ARGUMENTS", sanitized_args)

    if working == before_sub and auto_append and sanitized_args:
        working = f"{working}\n\nARGUMENTS: {sanitized_args}"

    return working.replace(_ESCAPED_DOLLAR_SENTINEL, "$")


def substitute_environment_variables(
    text: str,
    *,
    skill_dir: Path | None = None,
    project_dir: Path | None = None,
    session_id: str | None = None,
    effort: Any = None,
    plugin_root: Path | None = None,
    plugin_data_dir: Path | None = None,
) -> str:
    """Substitute skill, project, session, effort, and plugin environment variables."""
    result = text
    if skill_dir is not None:
        s_dir = str(skill_dir).replace("\\", "/")
        result = result.replace("${CLAUDE_SKILL_DIR}", s_dir)
        result = result.replace("${OPSCLOUD_SKILL_DIR}", s_dir)

    resolved_project = (
        str(project_dir)
        if project_dir is not None
        else os.environ.get("OPSCLOUD_PROJECT_DIR", os.environ.get("CLAUDE_PROJECT_DIR", ""))
    )
    if resolved_project:
        p_dir = resolved_project.replace("\\", "/")
        result = result.replace("${CLAUDE_PROJECT_DIR}", p_dir)
        result = result.replace("${OPSCLOUD_PROJECT_DIR}", p_dir)

    resolved_session = (
        session_id
        or os.environ.get("OPSCLOUD_SESSION_ID")
        or os.environ.get("CLAUDE_SESSION_ID")
        or ""
    )
    result = result.replace("${CLAUDE_SESSION_ID}", resolved_session)
    result = result.replace("${OPSCLOUD_SESSION_ID}", resolved_session)

    resolved_effort = (
        str(effort)
        if effort is not None
        else os.environ.get("OPSCLOUD_EFFORT", os.environ.get("CLAUDE_EFFORT", "default"))
    )
    result = result.replace("${CLAUDE_EFFORT}", resolved_effort)
    result = result.replace("${OPSCLOUD_EFFORT}", resolved_effort)

    if plugin_root is not None:
        p_root = str(plugin_root).replace("\\", "/")
        result = result.replace("${CLAUDE_PLUGIN_ROOT}", p_root)
        result = result.replace("${OPSCLOUD_PLUGIN_ROOT}", p_root)

    if plugin_data_dir is not None:
        p_data = str(plugin_data_dir).replace("\\", "/")
        result = result.replace("${CLAUDE_PLUGIN_DATA}", p_data)
        result = result.replace("${OPSCLOUD_PLUGIN_DATA}", p_data)

    return result


def _run_shell_snippet(
    command: str,
    *,
    cwd: Path | None = None,
    shell: ShellType = "bash",
    timeout_seconds: float = 30.0,
) -> str:
    """Execute a single shell command snippet and return combined stdout/stderr."""
    cmd_trimmed = command.strip()
    if not cmd_trimmed:
        return ""

    work_dir = cwd if cwd is not None and cwd.exists() else Path.cwd()
    if shell == "powershell":
        pwsh_bin = shutil.which("pwsh") or shutil.which("powershell")
        if pwsh_bin:
            argv = [pwsh_bin, "-NoProfile", "-NonInteractive", "-Command", cmd_trimmed]
        else:
            argv = ["/bin/sh", "-c", cmd_trimmed]
    else:
        bash_bin = shutil.which("bash") or "/bin/sh"
        argv = [bash_bin, "-c", cmd_trimmed]

    try:
        proc = subprocess.run(
            argv,
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        if stdout and stderr:
            return f"{stdout}\n{stderr}"
        return stdout or stderr
    except subprocess.TimeoutExpired:
        return f"[Error: inline command timed out after {timeout_seconds}s: {cmd_trimmed}]"
    except OSError as exc:
        return f"[Error executing inline command '{cmd_trimmed}': {exc}]"


def execute_inline_shell_commands(
    text: str,
    *,
    cwd: Path | None = None,
    shell: ShellType = "bash",
    timeout_seconds: float = 30.0,
    enabled: bool = True,
) -> str:
    """Execute fenced ```` ```! ... ``` ```` and inline `` !`cmd` `` blocks in-place.

    Protects regular markdown fenced code blocks and regular inline code spans so
    documentation examples containing `` !`...` `` inside normal code blocks are
    never executed.
    """
    if not enabled or ("!`" not in text and "```!" not in text):
        return text

    protected_blocks: list[str] = []

    def _protect_regular_fenced(match: re.Match[str]) -> str:
        idx = len(protected_blocks)
        protected_blocks.append(match.group(0))
        return f"\x00PROTECTED_CODE_BLOCK_{idx}\x00"

    working = _REGULAR_FENCED_CODE_RE.sub(_protect_regular_fenced, text)

    def _replace_fenced_shell(match: re.Match[str]) -> str:
        script = match.group(1)
        return _run_shell_snippet(script, cwd=cwd, shell=shell, timeout_seconds=timeout_seconds)

    working = _FENCED_SHELL_BLOCK_RE.sub(_replace_fenced_shell, working)

    def _protect_regular_inline(match: re.Match[str]) -> str:
        idx = len(protected_blocks)
        protected_blocks.append(match.group(0))
        return f"\x00PROTECTED_CODE_BLOCK_{idx}\x00"

    working = _REGULAR_INLINE_CODE_RE.sub(_protect_regular_inline, working)

    def _replace_inline_shell(match: re.Match[str]) -> str:
        cmd = match.group(1)
        return _run_shell_snippet(cmd, cwd=cwd, shell=shell, timeout_seconds=timeout_seconds)

    working = _INLINE_SHELL_RE.sub(_replace_inline_shell, working)
    working = working.replace(r"\!`", "!`")

    for idx, original in enumerate(protected_blocks):
        working = working.replace(f"\x00PROTECTED_CODE_BLOCK_{idx}\x00", original)

    return working


def extract_file_mentions(text: str) -> list[str]:
    """Extract `@path/to/file` mentions from expanded prompt text."""
    stripped = _REGULAR_FENCED_CODE_RE.sub("", text)
    stripped = _REGULAR_INLINE_CODE_RE.sub("", stripped)
    mentions: list[str] = []
    for match in _FILE_MENTION_RE.finditer(stripped):
        candidate = match.group(1).rstrip(".,;:!?)")
        if candidate and candidate not in mentions:
            mentions.append(candidate)
    return mentions


def format_command_xml_tags(command_name: str, args: str = "") -> str:
    """Format `<command-message>`, `<command-name>`, and `<command-args>` XML wrapper."""
    clean_name = command_name.lstrip("/")
    tags = [
        f"<command-message>{clean_name}</command-message>",
        f"<command-name>/{clean_name}</command-name>",
    ]
    trimmed_args = args.strip()
    if trimmed_args:
        tags.append(f"<command-args>{trimmed_args}</command-args>")
    return "\n".join(tags)


def expand_custom_command_prompt(
    definition: CustomCommandDefinition,
    args: str = "",
    *,
    cwd: Path | None = None,
    project_dir: Path | None = None,
    session_id: str | None = None,
    effort: Any = None,
    execute_shell: bool = True,
) -> ExpandedCommandPrompt:
    """Run the prompt expansion pipeline on `definition`."""
    prompt_text = definition.body
    if definition.base_dir is not None:
        prompt_text = f"Base directory for this skill: {definition.base_dir}\n\n{prompt_text}"

    prompt_text = substitute_arguments(
        prompt_text,
        args,
        definition.argument_names,
        auto_append=True,
    )

    effective_effort = definition.effort if definition.effort is not None else effort
    prompt_text = substitute_environment_variables(
        prompt_text,
        skill_dir=definition.base_dir,
        project_dir=project_dir,
        session_id=session_id,
        effort=effective_effort,
        plugin_root=definition.plugin_root,
        plugin_data_dir=definition.plugin_data_dir,
    )

    prompt_text = execute_inline_shell_commands(
        prompt_text,
        cwd=cwd or project_dir,
        shell=definition.shell,
        enabled=execute_shell,
    )

    referenced_files = extract_file_mentions(prompt_text)
    xml_message = format_command_xml_tags(definition.name, args)
    display_text = f"/{definition.name} {args}".strip() if args.strip() else f"/{definition.name}"

    permissions = CommandPermissionContext(
        command_name=definition.name,
        allowed_tools=definition.allowed_tools,
        disallowed_tools=definition.disallowed_tools,
        model=definition.model,
        effort=definition.effort,
        execution_context=definition.execution_context,
        agent=definition.agent,
        base_dir=str(definition.base_dir) if definition.base_dir else None,
    )

    return ExpandedCommandPrompt(
        command_name=definition.name,
        display_text=display_text,
        xml_command_message=xml_message,
        expanded_prompt=prompt_text.strip(),
        base_dir=definition.base_dir,
        referenced_files=referenced_files,
        permissions=permissions,
        execution_context=definition.execution_context,
        agent=definition.agent,
        background=definition.background,
        stacked_commands=[definition.name],
    )


def resolve_command_with_colon_fallback(
    name: str,
    args: str,
    command_lookup: Callable[[str], CustomCommandDefinition | None],
) -> tuple[CustomCommandDefinition, str] | None:
    """Resolve `/name args` with space-to-colon namespace fallback.

    If `/name` exists directly, returns `(def, args)`.
    Otherwise, if `args` starts with a subcommand token `sub` and `/name:sub`
    exists (recursively for multi-level namespaces like `/a b c` -> `/a:b:c`),
    returns the colon-namespaced command and remaining arguments.
    """
    clean_name = name.lstrip("/")
    direct = command_lookup(clean_name)
    if direct is not None:
        return direct, args

    remaining = args.strip()
    candidate_prefix = clean_name
    while remaining:
        parts = remaining.split(None, 1)
        first_tok = parts[0]
        rest = parts[1] if len(parts) > 1 else ""
        candidate_prefix = f"{candidate_prefix}:{first_tok}"
        matched = command_lookup(candidate_prefix)
        if matched is not None:
            return matched, rest
        remaining = rest

    return None


def peel_stacked_prompt_commands(
    raw_input: str,
    command_lookup: Callable[[str], CustomCommandDefinition | None],
    *,
    max_stack: int = MAX_STACKED_PROMPT_COMMANDS,
) -> list[tuple[CustomCommandDefinition, str]]:
    """Peel up to `max_stack` (5) chained `/cmd1 /cmd2 args` prompt commands.

    Tokens starting with `/` outside quotes that resolve to a known custom
    prompt command start a new command segment; non-command tokens are
    accumulated as arguments to the preceding command.
    """
    trimmed = raw_input.strip()
    if not trimmed.startswith("/"):
        return []

    # Split top-level space-delimited segments while respecting quotes
    segments: list[str] = []
    current: list[str] = []
    quote_char: str | None = None
    i = 0
    while i < len(trimmed):
        ch = trimmed[i]
        if ch == "\\" and i + 1 < len(trimmed):
            current.append(ch)
            current.append(trimmed[i + 1])
            i += 2
            continue
        if quote_char is not None:
            if ch == quote_char:
                quote_char = None
            current.append(ch)
        elif ch in ('"', "'"):
            quote_char = ch
            current.append(ch)
        elif ch in (" ", "\t") and current:
            segments.append("".join(current))
            current = []
        elif ch not in (" ", "\t"):
            current.append(ch)
        i += 1
    if current:
        segments.append("".join(current))

    if not segments:
        return []

    stacked: list[tuple[CustomCommandDefinition, str]] = []
    idx = 0
    while idx < len(segments) and len(stacked) < max_stack:
        tok = segments[idx]
        if not tok.startswith("/") or len(tok) <= 1:
            break

        cmd_token = tok[1:]
        # Try direct lookup or space-to-colon lookup with following non-slash tokens
        resolved_def = command_lookup(cmd_token)
        consumed = 1
        if resolved_def is None:
            prefix = cmd_token
            lookahead = idx + 1
            while lookahead < len(segments) and not segments[lookahead].startswith("/"):
                prefix = f"{prefix}:{segments[lookahead]}"
                candidate = command_lookup(prefix)
                if candidate is not None:
                    resolved_def = candidate
                    consumed = (lookahead - idx) + 1
                    break
                lookahead += 1

        if resolved_def is None:
            if not stacked:
                return []
            # Not a known command; treat remaining tokens as args to previous command
            prev_def, prev_args = stacked[-1]
            extra = " ".join(segments[idx:])
            stacked[-1] = (prev_def, f"{prev_args} {extra}".strip())
            break

        idx += consumed
        arg_tokens: list[str] = []
        while idx < len(segments):
            next_tok = segments[idx]
            if (
                next_tok.startswith("/")
                and len(next_tok) > 1
                and len(stacked) + 1 < max_stack
            ):
                # Check if next_tok is another valid custom command
                next_cmd_name = next_tok[1:]
                if command_lookup(next_cmd_name) is not None:
                    break
            arg_tokens.append(next_tok)
            idx += 1

        stacked.append((resolved_def, " ".join(arg_tokens).strip()))

    if idx < len(segments) and stacked:
        prev_def, prev_args = stacked[-1]
        extra = " ".join(segments[idx:])
        stacked[-1] = (prev_def, f"{prev_args} {extra}".strip())

    return stacked
