"""Output evaluator — regex pattern matching on final agent responses."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class OutputMatch:
    matched: bool
    score: float
    matched_patterns: list[str]
    unmatched_patterns: list[str]


def evaluate_output_patterns(output: str, patterns: list[str]) -> OutputMatch:
    matched = []
    unmatched = []
    for pat in patterns:
        if re.search(pat, output, re.IGNORECASE):
            matched.append(pat)
        else:
            unmatched.append(pat)

    score = len(matched) / len(patterns) if patterns else 1.0
    return OutputMatch(
        matched=len(unmatched) == 0,
        score=score,
        matched_patterns=matched,
        unmatched_patterns=unmatched,
    )
