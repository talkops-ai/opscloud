"""Content filter for internal protocol messages.

Detects and filters SystemMessage-echoed content that middleware injects
into the LLM conversation but should never surface to end users on
external platforms (Slack, Teams, etc.).

These patterns originate from:

- :class:`PlanLockMiddleware`     — ``ACTIVE PLAN (LOCKED — DO NOT DEVIATE)``
- :class:`TodoListMiddleware`     — ``Execution Checklist``, ``⏳ pending``
- Coordinator prompt sections     — ``[PLAN-APPROVED]``, ``[PLAN-LOCKED]``
- Duplicate guard                 — ``[DUPLICATE CALL BLOCKED]``

Filtered content is logged at DEBUG level with a ``[FILTERED]`` tag
for troubleshooting.
"""

from __future__ import annotations

import re

from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Compiled patterns — catch both full lines and substring echoes
# ---------------------------------------------------------------------------

_INTERNAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    # PlanLockMiddleware injected SystemMessages
    re.compile(r"##?\s*ACTIVE PLAN\s*\(LOCKED", re.IGNORECASE),
    re.compile(r"DO NOT DEVIATE\)?", re.IGNORECASE),
    re.compile(r"Any deviation is a protocol violation", re.IGNORECASE),
    re.compile(
        r"STOP and report the error\.?\s*Do NOT attempt alternatives",
        re.IGNORECASE,
    ),
    re.compile(
        r"The user approved the following plan\.\s*Execute\s+EXACTLY",
        re.IGNORECASE,
    ),
    # TodoList / Execution checklist
    re.compile(r"###?\s*Execution Checklist", re.IGNORECASE),
    re.compile(r"⏳\s*.*\(pending\)", re.IGNORECASE),
    re.compile(r"Update TODO status via\s+`?write_todos`?", re.IGNORECASE),
    re.compile(
        r"Execute the next.*pending/in_progress step",
        re.IGNORECASE,
    ),
    # Coordinator delegation prefixes
    re.compile(r"\[PLAN-APPROVED\]"),
    re.compile(r"\[PLAN-LOCKED\]"),
    re.compile(r"Delegate with \[PLAN-APPROVED\]", re.IGNORECASE),
    # Cross-domain / skill shortcut prefixes
    re.compile(r"\[CROSS-DOMAIN\].*Source:", re.IGNORECASE),
    re.compile(r"\[SKILL-EXISTS SHORTCUT\]"),
    # Duplicate guard
    re.compile(r"\[DUPLICATE CALL BLOCKED", re.IGNORECASE),
    # Protocol violation warnings echoed by LLM
    re.compile(r"protocol violation", re.IGNORECASE),
    # Rules section headers from plan-lock
    re.compile(r"###?\s*Rules\s*$", re.IGNORECASE),
)


def is_internal_message(text: str) -> bool:
    """Check whether *text* contains internal protocol content.

    This is a lightweight check intended for per-chunk screening.
    Returns ``True`` if **any** internal pattern matches anywhere in
    the text.

    >>> is_internal_message("## ACTIVE PLAN (LOCKED — DO NOT DEVIATE)")
    True
    >>> is_internal_message("Here are the pods in namespace default:")
    False
    """
    if not text:
        return False
    return any(p.search(text) for p in _INTERNAL_PATTERNS)


def filter_internal_content(text: str) -> str:
    r"""Remove internal protocol lines, return user-facing text.

    Operates line-by-line: any line matching an internal pattern is
    removed and logged at DEBUG level.  Returns the cleaned text
    (may be empty if all lines are internal).

    >>> filter_internal_content(
    ...     "## ACTIVE PLAN (LOCKED)\\nInstall argo-cd\\nDO NOT DEVIATE"
    ... )
    'Install argo-cd'
    """
    if not text:
        return ""

    lines = text.split("\n")
    clean_lines: list[str] = []

    for line in lines:
        if any(p.search(line) for p in _INTERNAL_PATTERNS):
            logger.debug("[FILTERED] %s", line[:120])
        else:
            clean_lines.append(line)

    result = "\n".join(clean_lines).strip()

    # Log summary when we actually filtered something
    if len(clean_lines) < len(lines):
        removed = len(lines) - len(clean_lines)
        logger.debug(
            "[FILTERED] Removed %d/%d internal lines from chunk",
            removed,
            len(lines),
        )

    return result
