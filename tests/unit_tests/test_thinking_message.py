"""Tests for the live/collapsed thinking display (``ThinkingMessage``)."""

from __future__ import annotations

from opscloud.ui.widgets.messages import ThinkingMessage, _thinking_heading

THOUGHT = """**Investigating pod restarts**

The user wants to know why payments-worker keeps restarting. I should check
the pod events first.

## Correlating with recent changes

The memory limit was lowered in commit a91f3c2."""


def test_heading_detection() -> None:
    assert _thinking_heading("**Checking rollout**") == "Checking rollout"
    assert _thinking_heading("### Checking rollout") == "Checking rollout"
    assert _thinking_heading("plain sentence with **bold** inside") is None
    assert _thinking_heading("") is None


def test_topic_is_first_heading() -> None:
    assert ThinkingMessage(THOUGHT).topic == "Investigating pod restarts"
    assert ThinkingMessage("no headings here").topic is None


def test_preview_is_latest_paragraph_flattened() -> None:
    msg = ThinkingMessage("**Step one**\n\nfirst line\nsecond line")
    assert msg.preview == "first line second line"
    assert ThinkingMessage(THOUGHT).preview == "The memory limit was lowered in commit a91f3c2."


def test_live_widget_ignores_external_duration_and_finishes() -> None:
    msg = ThinkingMessage(live=True)
    assert msg.is_live
    # The adapter's duration is turn-scoped; a live block times itself.
    msg.update_thinking("**Planning**", duration_seconds=99.0)
    assert msg._duration_seconds == 0.0
    msg.finish()
    assert not msg.is_live
    assert msg._duration_seconds < 99.0
    # Finishing twice is a no-op.
    msg.finish()
    assert not msg.is_live


def test_history_widget_uses_given_duration() -> None:
    msg = ThinkingMessage(THOUGHT, duration_seconds=6.4)
    assert not msg.is_live
    assert msg._duration_seconds == 6.4


def test_toggle_details_only_when_finished_with_content() -> None:
    live = ThinkingMessage("**x**", live=True)
    live.toggle_details()
    assert live._expanded is False  # cannot expand while streaming

    empty = ThinkingMessage("")
    empty.toggle_details()
    assert empty._expanded is False
