"""Unit tests for goal state size limits and validators."""

import pytest
from opscloud.state.goal_state_limits import (
    GOAL_APPLICATION_CHAR_LIMIT,
    GOAL_NOTICE_TEXT_CHAR_LIMIT,
    GOAL_OBJECTIVE_CHAR_LIMIT,
    GOAL_STATUS_NOTE_CHAR_LIMIT,
    RUBRIC_CHAR_LIMIT,
    GoalStateSizeError,
    validate_goal_application,
    validate_goal_objective,
    validate_goal_status_note,
    validate_rubric,
)


def test_validate_goal_objective():
    # Valid
    validate_goal_objective("Deploy VPC with 3 public and 3 private subnets")

    # Empty / whitespace
    with pytest.raises(ValueError, match="must contain non-whitespace text"):
        validate_goal_objective("   \n\t  ")

    # Exceeding limit
    oversized = "x" * (GOAL_OBJECTIVE_CHAR_LIMIT + 1)
    with pytest.raises(GoalStateSizeError) as exc_info:
        validate_goal_objective(oversized)
    assert exc_info.value.limit == GOAL_OBJECTIVE_CHAR_LIMIT
    assert exc_info.value.actual == GOAL_OBJECTIVE_CHAR_LIMIT + 1


def test_validate_rubric():
    # Valid
    validate_rubric("- Terraform plan has 0 errors\n- S3 bucket versioning is enabled")

    # Empty / whitespace
    with pytest.raises(ValueError, match="must contain non-whitespace text"):
        validate_rubric("   ")

    # Exceeding limit
    oversized = "x" * (RUBRIC_CHAR_LIMIT + 1)
    with pytest.raises(GoalStateSizeError) as exc_info:
        validate_rubric(oversized)
    assert exc_info.value.limit == RUBRIC_CHAR_LIMIT
    assert exc_info.value.actual == RUBRIC_CHAR_LIMIT + 1


def test_validate_goal_status_note():
    # Valid
    validate_goal_status_note("All unit tests pass and terraform validate succeeded.")

    # Empty / whitespace
    with pytest.raises(ValueError, match="must contain non-whitespace text"):
        validate_goal_status_note("")

    # Exceeding limit
    oversized = "y" * (GOAL_STATUS_NOTE_CHAR_LIMIT + 1)
    with pytest.raises(GoalStateSizeError) as exc_info:
        validate_goal_status_note(oversized)
    assert exc_info.value.limit == GOAL_STATUS_NOTE_CHAR_LIMIT


def test_validate_goal_application():
    obj = "Set up EKS cluster with managed node groups"
    crit = "- Cluster status is ACTIVE\n- Node groups have 2 ready nodes"
    validate_goal_application(obj, crit)

    # Exceeding combined budget
    huge_crit = "c" * (GOAL_APPLICATION_CHAR_LIMIT - 10)
    huge_obj = "o" * 20
    with pytest.raises(GoalStateSizeError) as exc_info:
        validate_goal_application(huge_obj, huge_crit)
    assert exc_info.value.limit == GOAL_APPLICATION_CHAR_LIMIT
