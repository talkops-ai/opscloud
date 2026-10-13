"""Tests for the per-turn summary footer (``TurnSummary``)."""

from __future__ import annotations

import pytest

from opscloud.ui.widgets.messages import TurnSummary, _format_turn_duration


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0.0, "0.0s"),
        (4.24, "4.2s"),
        (59.9, "59.9s"),
        (125, "2m 05s"),
        (3725, "1h 02m"),
        (-3, "0.0s"),
    ],
)
def test_format_turn_duration(seconds: float, expected: str) -> None:
    assert _format_turn_duration(seconds) == expected


def test_summary_includes_all_nonzero_metrics() -> None:
    summary = TurnSummary(elapsed_seconds=12.4, tool_count=4, tokens=3200, cost_usd=0.02)
    text = summary.summary_text()
    assert text.startswith("Done in 12.4s")
    assert "4 tools" in text
    assert "3.2K tokens" in text
    assert "$0.02" in text


def test_summary_omits_zero_metrics_and_singularises() -> None:
    assert TurnSummary(elapsed_seconds=1.0).summary_text() == "Done in 1.0s"
    text = TurnSummary(elapsed_seconds=1.0, tool_count=1).summary_text()
    assert "1 tool" in text and "1 tools" not in text


def test_summary_clamps_negative_deltas() -> None:
    # Deltas can go negative if the thread's counters are reset mid-turn.
    summary = TurnSummary(elapsed_seconds=2.0, tool_count=-1, tokens=-50, cost_usd=-0.1)
    assert summary.summary_text() == "Done in 2.0s"


def test_summary_is_not_selectable() -> None:
    # Copying a reply should never pick up the footer line.
    assert TurnSummary.ALLOW_SELECT is False
