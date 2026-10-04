"""Trajectory evaluator — deterministic checks on agent tool call sequences."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TrajectoryMatch:
    matched: bool
    score: float
    expected_tools: list[str]
    actual_tools: list[str]
    missing_tools: list[str] = field(default_factory=list)


def evaluate_tool_sequence(
    actual_tool_calls: list[dict[str, Any]],
    expected_tool_names: list[str],
) -> TrajectoryMatch:
    actual_names = [tc.get("name", "") for tc in actual_tool_calls]
    actual_set = set(actual_names)
    expected_set = set(expected_tool_names)
    missing = expected_set - actual_set

    matched = len(missing) == 0
    score = (len(expected_set) - len(missing)) / len(expected_set) if expected_set else 1.0

    return TrajectoryMatch(
        matched=matched,
        score=score,
        expected_tools=expected_tool_names,
        actual_tools=actual_names,
        missing_tools=list(missing),
    )
