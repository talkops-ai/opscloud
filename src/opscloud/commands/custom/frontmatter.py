"""YAML frontmatter parser for custom commands and skills.

Provides frontmatter extraction, resilient malformed-YAML recovery, brace
expansion, tool permission parsing, and argument-name parsing.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from opscloud.commands.custom.models import (
    CustomCommandFrontmatter,
    EffortValue,
    ExecutionContextMode,
    ShellType,
)

_FRONTMATTER_RE = re.compile(r"^---\s*\n([\s\S]*?)---\s*(?:\n|$)")
_ARG_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_-]*$")
_HEADING_RE = re.compile(r"^#+\s*(.+)$")
_UNQUOTED_LINE_RE = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_-]*)\s*:\s*(.+)$")

_MAX_BRACE_EXPANSIONS = 100
_MAX_BRACE_DEPTH = 10


def _recover_malformed_yaml(yaml_text: str) -> str:
    """Normalize tabs and wrap unquoted scalar values containing colons."""
    lines = yaml_text.replace("\t", "  ").splitlines()
    recovered: list[str] = []
    for line in lines:
        m = _UNQUOTED_LINE_RE.match(line)
        if not m:
            recovered.append(line)
            continue
        key, val = m.group(1), m.group(2).strip()
        if (
            not val
            or val.startswith(('"', "'", "[", "{", "|", ">"))
            or ":" not in val
        ):
            recovered.append(line)
            continue
        escaped = val.replace("\\", "\\\\").replace('"', '\\"')
        recovered.append(f'{key}: "{escaped}"')
    return "\n".join(recovered)


def parse_frontmatter(raw_content: str) -> tuple[dict[str, Any], str]:
    """Extract YAML frontmatter dictionary and markdown body.

    Includes BOM stripping and automatic fallback recovery when unquoted colons
    or tabs appear in user-authored frontmatter.
    """
    cleaned = raw_content.lstrip("\ufeff")
    match = _FRONTMATTER_RE.match(cleaned)
    if not match:
        return {}, cleaned

    yaml_block = match.group(1)
    body = cleaned[match.end() :]

    try:
        parsed = yaml.safe_load(yaml_block)
        if isinstance(parsed, dict):
            return {str(k): v for k, v in parsed.items()}, body
    except yaml.YAMLError:
        try:
            recovered_block = _recover_malformed_yaml(yaml_block)
            parsed = yaml.safe_load(recovered_block)
            if isinstance(parsed, dict):
                return {str(k): v for k, v in parsed.items()}, body
        except yaml.YAMLError:
            pass

    return {}, body


def expand_braces(pattern: str, _depth: int = 0) -> list[str]:
    """Expand shell-style `{a,b}` braces in tool specs.

    Respects `_MAX_BRACE_DEPTH` (10) and `_MAX_BRACE_EXPANSIONS` (100).
    """
    if _depth >= _MAX_BRACE_DEPTH:
        return [pattern]

    open_idx = pattern.find("{")
    if open_idx == -1:
        return [pattern]

    depth = 0
    close_idx = -1
    for i in range(open_idx, len(pattern)):
        if pattern[i] == "{":
            depth += 1
        elif pattern[i] == "}":
            depth -= 1
            if depth == 0:
                close_idx = i
                break

    if close_idx == -1:
        return [pattern]

    prefix = pattern[:open_idx]
    suffix = pattern[close_idx + 1 :]
    inner = pattern[open_idx + 1 : close_idx]

    parts: list[str] = []
    part_start = 0
    nested = 0
    for i, ch in enumerate(inner):
        if ch == "{":
            nested += 1
        elif ch == "}":
            nested -= 1
        elif ch == "," and nested == 0:
            parts.append(inner[part_start:i])
            part_start = i + 1
    parts.append(inner[part_start:])

    if len(parts) < 2:
        return [pattern]

    results: list[str] = []
    for part in parts:
        expanded = expand_braces(f"{prefix}{part}{suffix}", _depth + 1)
        for item in expanded:
            results.append(item)
            if len(results) >= _MAX_BRACE_EXPANSIONS:
                return results
    return results


def _split_tool_tokens(raw_str: str) -> list[str]:
    """Split a comma- or space-separated tool string while preserving `Bash(git *)` parentheses."""
    tokens: list[str] = []
    current: list[str] = []
    paren_depth = 0
    brace_depth = 0

    for ch in raw_str.strip():
        if ch == "(":
            paren_depth += 1
            current.append(ch)
        elif ch == ")":
            paren_depth = max(0, paren_depth - 1)
            current.append(ch)
        elif ch == "{":
            brace_depth += 1
            current.append(ch)
        elif ch == "}":
            brace_depth = max(0, brace_depth - 1)
            current.append(ch)
        elif ch in (",", " ", "\t", "\n") and paren_depth == 0 and brace_depth == 0:
            token = "".join(current).strip()
            if token:
                tokens.append(token)
            current = []
        else:
            current.append(ch)

    tail = "".join(current).strip()
    if tail:
        tokens.append(tail)
    return tokens


def _substitute_tool_dir_vars(
    tool_spec: str,
    skill_dir: Path | None = None,
    project_dir: Path | None = None,
) -> str:
    """Substitute `${CLAUDE_SKILL_DIR}` / `${OPSCLOUD_SKILL_DIR}` and project dir in tool specs."""
    result = tool_spec
    if skill_dir is not None:
        s_dir = str(skill_dir).replace("\\", "/")
        result = result.replace("${CLAUDE_SKILL_DIR}", s_dir)
        result = result.replace("${OPSCLOUD_SKILL_DIR}", s_dir)
    proj = str(project_dir) if project_dir is not None else os.environ.get(
        "OPSCLOUD_PROJECT_DIR", os.environ.get("CLAUDE_PROJECT_DIR", "")
    )
    if proj:
        p_dir = proj.replace("\\", "/")
        result = result.replace("${CLAUDE_PROJECT_DIR}", p_dir)
        result = result.replace("${OPSCLOUD_PROJECT_DIR}", p_dir)
    return result


def parse_tool_permissions(
    raw_value: Any,
    skill_dir: Path | None = None,
    project_dir: Path | None = None,
) -> list[str]:
    """Parse `allowed-tools` or `disallowed-tools` frontmatter into expanded tool rule strings."""
    if not raw_value:
        return []

    raw_items: list[str] = []
    if isinstance(raw_value, str):
        raw_items = _split_tool_tokens(raw_value)
    elif isinstance(raw_value, list):
        for item in raw_value:
            if isinstance(item, str):
                raw_items.extend(_split_tool_tokens(item))

    expanded_tools: list[str] = []
    for item in raw_items:
        substituted = _substitute_tool_dir_vars(item.strip(), skill_dir=skill_dir, project_dir=project_dir)
        if not substituted:
            continue
        for exp in expand_braces(substituted):
            cleaned = exp.strip()
            if cleaned and cleaned not in expanded_tools:
                expanded_tools.append(cleaned)
    return expanded_tools


def parse_argument_names(raw_value: Any) -> list[str]:
    """Parse named positional arguments from `arguments` frontmatter."""
    if raw_value is None:
        return []
    candidates: list[str] = []
    if isinstance(raw_value, list):
        candidates = [str(x).strip() for x in raw_value if str(x).strip()]
    elif isinstance(raw_value, (str, int, float)):
        candidates = [tok for tok in str(raw_value).strip().split() if tok]
    return [c for c in candidates if _ARG_IDENT_RE.match(c)]


def parse_paths_frontmatter(raw_value: Any) -> list[str]:
    """Parse `paths` frontmatter into a list of glob patterns."""
    if not raw_value:
        return []
    raw_list: list[str] = []
    if isinstance(raw_value, list):
        raw_list = [str(x).strip() for x in raw_value if isinstance(x, str)]
    elif isinstance(raw_value, str):
        raw_list = [part.strip() for part in raw_value.split(",")]

    results: list[str] = []
    for pat in raw_list:
        if not pat or pat in ("**", "**/*"):
            continue
        results.append(pat[:-3] if pat.endswith("/**") else pat)
    return results


def parse_effort_value(raw_value: Any) -> EffortValue | None:
    """Parse `effort` frontmatter: `low`, `medium`, `high`, `max`, or integer."""
    if raw_value is None:
        return None
    if isinstance(raw_value, int) and not isinstance(raw_value, bool):
        return raw_value
    if isinstance(raw_value, str):
        stripped = raw_value.strip().lower()
        if stripped in ("low", "medium", "high", "max"):
            return stripped  # type: ignore[return-value]
        if stripped.isdigit():
            return int(stripped)
    return None


def parse_boolean_frontmatter(raw_value: Any, default: bool = False) -> bool:
    """Parse boolean frontmatter field."""
    if raw_value is None:
        return default
    if isinstance(raw_value, bool):
        return raw_value
    if isinstance(raw_value, str):
        low = raw_value.strip().lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no"):
            return False
    return default


def extract_description_from_markdown(
    body: str,
    fallback_label: str = "Custom command",
) -> str:
    """Extract fallback description from the first non-empty markdown line."""
    for line in body.splitlines():
        trimmed = line.strip()
        if not trimmed:
            continue
        heading_match = _HEADING_RE.match(trimmed)
        text = heading_match.group(1).strip() if heading_match else trimmed
        if len(text) > 100:
            return f"{text[:97]}..."
        return text
    return fallback_label


def parse_custom_command_frontmatter(
    raw_content: str,
    *,
    skill_dir: Path | None = None,
    project_dir: Path | None = None,
    fallback_label: str = "Custom command",
) -> tuple[CustomCommandFrontmatter, str, str, bool]:
    """Parse a markdown file into `(frontmatter, body, description, has_user_specified_description)`."""
    fm_dict, body = parse_frontmatter(raw_content)

    raw_name = fm_dict.get("name") or fm_dict.get("display_name")
    name_str = str(raw_name).strip() if raw_name else None

    raw_desc = fm_dict.get("description")
    has_user_desc = bool(raw_desc and str(raw_desc).strip())
    description = (
        str(raw_desc).strip()
        if has_user_desc
        else extract_description_from_markdown(body, fallback_label=fallback_label)
    )

    allowed_raw = (
        fm_dict.get("allowed-tools")
        or fm_dict.get("allowed_tools")
        or fm_dict.get("allowedTools")
    )
    disallowed_raw = (
        fm_dict.get("disallowed-tools")
        or fm_dict.get("disallowed_tools")
        or fm_dict.get("disallowedTools")
    )

    arg_hint_raw = fm_dict.get("argument-hint") or fm_dict.get("argument_hint")
    arg_hint: str | None = None
    if isinstance(arg_hint_raw, list):
        arg_hint = " ".join(f"[{x}]" if not str(x).startswith("[") else str(x) for x in arg_hint_raw)
    elif arg_hint_raw is not None:
        arg_hint = str(arg_hint_raw).strip() or None

    arguments = parse_argument_names(fm_dict.get("arguments"))

    raw_model = fm_dict.get("model")
    model_str = str(raw_model).strip() if isinstance(raw_model, str) and raw_model.strip() else None

    effort_val = parse_effort_value(fm_dict.get("effort"))

    raw_context = str(fm_dict.get("context", "inline")).strip().lower()
    context_mode: ExecutionContextMode = "fork" if raw_context == "fork" else "inline"

    raw_agent = fm_dict.get("agent")
    agent_str = str(raw_agent).strip() if isinstance(raw_agent, str) and raw_agent.strip() else None

    background = parse_boolean_frontmatter(fm_dict.get("background"), default=False)
    disable_model_invocation = parse_boolean_frontmatter(
        fm_dict.get("disable-model-invocation")
        if "disable-model-invocation" in fm_dict
        else fm_dict.get("disable_model_invocation", fm_dict.get("disableModelInvocation")),
        default=False,
    )
    user_invocable = parse_boolean_frontmatter(
        fm_dict.get("user-invocable")
        if "user-invocable" in fm_dict
        else fm_dict.get("user_invocable", fm_dict.get("userInvocable")),
        default=True,
    )

    raw_when = fm_dict.get("when_to_use") or fm_dict.get("when-to-use")
    when_to_use = str(raw_when).strip() if raw_when and str(raw_when).strip() else None

    paths = parse_paths_frontmatter(fm_dict.get("paths"))

    raw_shell = str(fm_dict.get("shell", "bash")).strip().lower()
    shell_type: ShellType = "powershell" if raw_shell in ("powershell", "pwsh") else "bash"

    raw_hooks = fm_dict.get("hooks")
    hooks = dict(raw_hooks) if isinstance(raw_hooks, dict) else {}

    raw_version = fm_dict.get("version")
    version_str = str(raw_version).strip() if raw_version is not None else None

    frontmatter = CustomCommandFrontmatter(
        name=name_str,
        description=description,
        allowed_tools=parse_tool_permissions(allowed_raw, skill_dir=skill_dir, project_dir=project_dir),
        disallowed_tools=parse_tool_permissions(
            disallowed_raw, skill_dir=skill_dir, project_dir=project_dir
        ),
        argument_hint=arg_hint,
        arguments=arguments,
        model=model_str,
        effort=effort_val,
        context=context_mode,
        agent=agent_str,
        background=background,
        disable_model_invocation=disable_model_invocation,
        user_invocable=user_invocable,
        when_to_use=when_to_use,
        paths=paths,
        shell=shell_type,
        hooks=hooks,
        version=version_str,
        raw=fm_dict,
    )
    return frontmatter, body, description, has_user_desc
