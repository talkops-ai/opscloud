"""System prompt templates and character-budgeted listing formatter for custom commands and skills."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from opscloud.commands.custom.loader import get_command_usage_priority
from opscloud.commands.custom.models import CustomCommandDefinition

# Character-budget allocation constants for custom command and skill listings
MAX_LISTING_DESC_CHARS = 250
CONTEXT_CHAR_BUDGET_RATIO = 0.01
CHARS_PER_TOKEN_ESTIMATE = 4
DEFAULT_CHAR_BUDGET = 8000
MIN_DESC_LENGTH = 20

SKILL_TOOL_PROMPT_HEADER = """Execute a skill within the main conversation

When users ask you to perform tasks, check if any of the available skills match. Skills provide specialized capabilities and domain knowledge.

When users ask you to run a "slash command" or reference "/<something>" (e.g., "/commit", "/review-pr"), they are referring to a skill. Use this tool to invoke it.

How to invoke:
- Use this tool with the skill name and optional arguments
- Examples:
  - `skill: "pdf"` - invoke the pdf skill
  - `skill: "commit", args: "-m 'Fix bug'"` - invoke with arguments
  - `skill: "review-pr", args: "123"` - invoke with arguments
  - `skill: "ms-office-suite:pdf"` - invoke using fully qualified name

Important:
- Explicit `/<skill-name>` in a user message (e.g., "/commit") is a direct command to invoke that skill immediately — BEFORE doing anything else, including responding, asking clarifying questions, reading files, or Exploring. Do not preface the tool call with conversational text; call the Skill tool as your first action. The skill's prompt contains the workflow instructions and defines how to handle questions or arguments.
- For other tasks, if a skill matches the user's request, this is a BLOCKING REQUIREMENT: invoke the relevant Skill tool BEFORE generating any other response about the task
- NEVER mention a skill without actually calling this tool
- Do not invoke a skill that is already running
- Do not use this tool for built-in CLI commands (like /help, /clear, etc.)
- If you see a <command-name> tag in the current conversation turn (e.g., <command-name>/commit</command-name>), the skill has ALREADY been loaded and its instructions follow in the next message. Do NOT call the Skill tool for that skill again — follow the instructions directly.
"""


def compute_command_char_budget(context_window_tokens: int | None = None) -> int:
    """Compute character budget for custom command/skill listings.

    Honors `SLASH_COMMAND_TOOL_CHAR_BUDGET` or `OPSCLOUD_COMMAND_CHAR_BUDGET`
    env var override; otherwise allocates 1% of context window characters
    (defaulting to 8,000 chars).
    """
    env_override = os.environ.get("SLASH_COMMAND_TOOL_CHAR_BUDGET") or os.environ.get(
        "OPSCLOUD_COMMAND_CHAR_BUDGET"
    )
    if env_override and env_override.strip().isdigit():
        return int(env_override.strip())
    if context_window_tokens and context_window_tokens > 0:
        return int(context_window_tokens * CHARS_PER_TOKEN_ESTIMATE * CONTEXT_CHAR_BUDGET_RATIO)
    return DEFAULT_CHAR_BUDGET


def _raw_command_description(cmd: CustomCommandDefinition) -> str:
    """Build combined `description - when_to_use` string."""
    if cmd.when_to_use:
        return f"{cmd.description} - {cmd.when_to_use}"
    return cmd.description


def format_single_command_entry(
    cmd: CustomCommandDefinition,
    max_desc_chars: int = MAX_LISTING_DESC_CHARS,
) -> str:
    """Format a single `- name: description` entry."""
    raw_desc = _raw_command_description(cmd)
    if len(raw_desc) > max_desc_chars:
        truncated = f"{raw_desc[: max_desc_chars - 1]}…"
    else:
        truncated = raw_desc
    if cmd.argument_hint:
        return f"- {cmd.name} ({cmd.argument_hint}): {truncated}"
    return f"- {cmd.name}: {truncated}"


def format_commands_for_budget(
    commands: Iterable[CustomCommandDefinition],
    *,
    context_window_tokens: int | None = None,
    usage_path: Path | None = None,
) -> str:
    """Format model-invocable custom commands/skills within the character budget.

    1. Filters out commands with `disable_model_invocation=True` unless the user
       explicitly configured them or they are model-invocable.
    2. Reserves budget for `bundled` commands first, then sorts remaining commands
       by recency-weighted usage priority (`get_command_usage_priority`).
    3. Dynamically scales description truncation length or omits lowest-priority
       descriptions if the total exceeds the budget.
    """
    eligible = [c for c in commands if not c.disable_model_invocation]
    if not eligible:
        return ""

    budget = compute_command_char_budget(context_window_tokens)
    full_entries = [(c, format_single_command_entry(c, MAX_LISTING_DESC_CHARS)) for c in eligible]
    total_chars = sum(len(entry) for _, entry in full_entries) + max(0, len(full_entries) - 1)
    if total_chars <= budget:
        return "\n".join(entry for _, entry in full_entries)

    bundled: list[CustomCommandDefinition] = []
    rest: list[tuple[CustomCommandDefinition, float]] = []
    for cmd in eligible:
        if cmd.source == "bundled" or cmd.loaded_from == "bundled":
            bundled.append(cmd)
        else:
            rest.append((cmd, get_command_usage_priority(cmd.name, usage_path=usage_path)))

    # Sort non-bundled by priority descending
    rest.sort(key=lambda item: item[1], reverse=True)
    ordered_rest = [item[0] for item in rest]

    bundled_entries = {id(c): format_single_command_entry(c, MAX_LISTING_DESC_CHARS) for c in bundled}
    bundled_chars = sum(len(v) + 1 for v in bundled_entries.values())
    remaining_budget = max(0, budget - bundled_chars)

    if not ordered_rest:
        return "\n".join(bundled_entries[id(c)] for c in eligible if id(c) in bundled_entries)

    overhead = sum(len(f"- {c.name}: ") + 1 for c in ordered_rest) - 1
    avail_for_desc = remaining_budget - overhead
    dynamic_desc_len = avail_for_desc // len(ordered_rest)

    if dynamic_desc_len >= MIN_DESC_LENGTH:
        capped_len = min(dynamic_desc_len, MAX_LISTING_DESC_CHARS)
        lines: list[str] = []
        for cmd in eligible:
            if id(cmd) in bundled_entries:
                lines.append(bundled_entries[id(cmd)])
            else:
                lines.append(format_single_command_entry(cmd, capped_len))
        return "\n".join(lines)

    # Budget too tight even at MIN_DESC_LENGTH: include top priority commands with MIN_DESC_LENGTH
    # and list remaining command names only
    included_with_desc: set[int] = set()
    used_chars = 0
    for cmd in ordered_rest:
        entry = format_single_command_entry(cmd, MIN_DESC_LENGTH)
        cost = len(entry) + (1 if used_chars > 0 else 0)
        name_only_reserve = sum(
            len(f"- {other.name}") + 1
            for other in ordered_rest
            if id(other) not in included_with_desc and other is not cmd
        )
        if used_chars + cost + name_only_reserve <= remaining_budget:
            included_with_desc.add(id(cmd))
            used_chars += cost

    lines = []
    for cmd in eligible:
        if id(cmd) in bundled_entries:
            lines.append(bundled_entries[id(cmd)])
        elif id(cmd) in included_with_desc:
            lines.append(format_single_command_entry(cmd, MIN_DESC_LENGTH))
        else:
            lines.append(f"- {cmd.name}")
    return "\n".join(lines)


def build_skill_tool_prompt(
    commands: Iterable[CustomCommandDefinition],
    *,
    context_window_tokens: int | None = None,
    usage_path: Path | None = None,
) -> str:
    """Build the complete `Skill` tool description prompt."""
    listing = format_commands_for_budget(
        commands,
        context_window_tokens=context_window_tokens,
        usage_path=usage_path,
    )
    if not listing:
        return SKILL_TOOL_PROMPT_HEADER.strip()
    return f"{SKILL_TOOL_PROMPT_HEADER.strip()}\n\nAvailable skills:\n{listing}"
