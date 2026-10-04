"""Shared size limits and validation for OpsCloud goal and rubric state.

Defines model-visible character budgets and validation routines to protect
conversation context windows from oversized prompts, criteria, or status notes.
"""

from __future__ import annotations

import html
from typing import Final, Literal, get_args

GoalStatus = Literal["active", "paused", "blocked", "complete"]
"""Lifecycle status of an OpsCloud goal."""

GOAL_STATUS_VALUES: Final[frozenset[str]] = frozenset(get_args(GoalStatus))
"""All recognized goal status literals."""

GOAL_OBJECTIVE_CHAR_LIMIT: Final = 8_000
"""Maximum character length for a single goal objective."""

RUBRIC_CHAR_LIMIT: Final = 12_000
"""Maximum character length for rubric / acceptance criteria text."""

GOAL_APPLICATION_CHAR_LIMIT: Final = 12_000
"""Maximum combined character length for an accepted objective and its criteria."""

GOAL_STATUS_NOTE_CHAR_LIMIT: Final = 4_000
"""Maximum character length for completion evidence or blocker notes."""

GOAL_NOTICE_TEXT_CHAR_LIMIT: Final = (
    GOAL_APPLICATION_CHAR_LIMIT + GOAL_STATUS_NOTE_CHAR_LIMIT
)
"""Maximum combined rendered characters allowed in a single goal-state notice."""

GoalStateSizeLabel = Literal[
    "Goal objective",
    "Rubric",
    "Goal status note",
    "Prior blocker",
    "Goal objective and criteria combined",
    "Goal-state notice text",
]
"""Allowed labels for GoalStateSizeError."""

GoalStatusNoteLabel = Literal["Goal status note", "Prior blocker"]
"""Allowed labels for status note size validation."""


class GoalStateSizeError(ValueError):
    """Raised when goal or rubric content exceeds configured context limits."""

    def __init__(
        self,
        label: GoalStateSizeLabel,
        actual: int,
        limit: int,
    ) -> None:
        if actual <= limit:
            msg = (
                f"GoalStateSizeError needs actual > limit; got "
                f"actual={actual}, limit={limit} for {label!r}."
            )
            raise ValueError(msg)
        self.label = label
        self.actual = actual
        self.limit = limit
        self.excess = actual - limit
        super().__init__(
            f"{label} is {actual:,} characters; maximum is {limit:,}. "
            f"Remove at least {self.excess:,} characters."
        )


def validate_goal_objective(text: str) -> None:
    """Validate that the goal objective does not exceed its character limit."""
    if not text.strip():
        msg = "must contain non-whitespace text"
        raise ValueError(msg)
    if len(text) > GOAL_OBJECTIVE_CHAR_LIMIT:
        raise GoalStateSizeError("Goal objective", len(text), GOAL_OBJECTIVE_CHAR_LIMIT)


def validate_rubric(text: str) -> None:
    """Validate that the rubric criteria text does not exceed its character limit."""
    if not text.strip():
        msg = "must contain non-whitespace text"
        raise ValueError(msg)
    if len(text) > RUBRIC_CHAR_LIMIT:
        raise GoalStateSizeError("Rubric", len(text), RUBRIC_CHAR_LIMIT)


def validate_goal_status_note(
    text: str,
    label: GoalStatusNoteLabel = "Goal status note",
) -> None:
    """Validate that a completion note or blocker note does not exceed limits."""
    if not text.strip():
        msg = "must contain non-whitespace text"
        raise ValueError(msg)
    if len(text) > GOAL_STATUS_NOTE_CHAR_LIMIT:
        raise GoalStateSizeError(label, len(text), GOAL_STATUS_NOTE_CHAR_LIMIT)


def validate_goal_application_total(objective: str, criteria: str) -> None:
    """Validate the aggregate length of objective and criteria combined."""
    total = len(objective) + len(criteria)
    if total > GOAL_APPLICATION_CHAR_LIMIT:
        raise GoalStateSizeError(
            "Goal objective and criteria combined",
            total,
            GOAL_APPLICATION_CHAR_LIMIT,
        )


def validate_goal_application_rendered_total(objective: str, criteria: str) -> None:
    """Validate the escaped size an accepted pair will occupy in the notice."""
    total = len(html.escape(objective, quote=False)) + len(
        html.escape(criteria, quote=False)
    )
    if total > GOAL_NOTICE_TEXT_CHAR_LIMIT:
        raise GoalStateSizeError(
            "Goal objective and criteria combined",
            total,
            GOAL_NOTICE_TEXT_CHAR_LIMIT,
        )


def validate_goal_objective_rendered(objective: str) -> None:
    """Reject an objective whose escaped text alone cannot fit a notice."""
    rendered = len(html.escape(objective, quote=False))
    if rendered >= GOAL_NOTICE_TEXT_CHAR_LIMIT:
        raise GoalStateSizeError(
            "Goal objective",
            rendered,
            GOAL_NOTICE_TEXT_CHAR_LIMIT - 1,
        )


def validate_goal_application(objective: str, criteria: str) -> None:
    """Validate individual and aggregate sizes for an objective-criteria pair."""
    validate_goal_objective(objective)
    validate_rubric(criteria)
    validate_goal_application_total(objective, criteria)
    validate_goal_application_rendered_total(objective, criteria)


def validate_goal_notice_text(
    *,
    objective: str | None,
    criteria: str | None,
    status_note: str | None,
    prior_blocker: str | None = None,
) -> None:
    """Validate every user-controlled section of a goal-state notice."""
    if objective is not None and criteria is not None:
        validate_goal_application(objective, criteria)
    elif objective is not None:
        validate_goal_objective(objective)
    elif criteria is not None:
        validate_rubric(criteria)
    if status_note is not None:
        validate_goal_status_note(status_note)
    if prior_blocker is not None:
        validate_goal_status_note(prior_blocker, label="Prior blocker")
    total = sum(
        len(html.escape(value, quote=False))
        for value in (objective, criteria, status_note, prior_blocker)
        if value is not None
    )
    if total > GOAL_NOTICE_TEXT_CHAR_LIMIT:
        raise GoalStateSizeError(
            "Goal-state notice text",
            total,
            GOAL_NOTICE_TEXT_CHAR_LIMIT,
        )
