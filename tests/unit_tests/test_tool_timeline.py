"""Tests for the refined tool-call timeline (Change 7).

Covers the structured header parts, the styled header for each lifecycle
state, de-duplication of header-visible args / command echo, result-summary
pluralisation and the group summary's count emphasis / live line.
"""

from __future__ import annotations

import time

import pytest

from opscloud.ui.widgets.messages import (
    ToolCallMessage,
    ToolGroupSummary,
    _arg_shown_in_header,
    _emphasize_counts,
)
from opscloud.ui.widgets.tool_display import (
    format_tool_display,
    format_tool_parts,
    format_tool_result_summary,
)


def _span_styles(content) -> list[str]:
    return [str(span.style) for span in content.spans]


# -- tool_display ----------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "args", "expected"),
    [
        ("read_file", {"file_path": "README.md"}, "● Read(README.md)"),
        ("execute", {"command": "kubectl get pods"}, '● Execute("kubectl get pods")'),
        ("grep", {"pattern": "OOM", "path": "logs/"}, '● Grep("OOM" in logs/)'),
        ("task", {"subagent_type": "sre"}, "● task [sre]"),
        ("ask_user", {"questions": [1, 2]}, "● Ask(2 questions)"),
        ("deploy_chart", {"charts": [1, 2, 3]}, "● DeployChart(3 items)"),
        ("noop", {}, "● Noop()"),
        (
            "plugin__aws-compute_talkops__describe_instances_f0b5243c096e",
            {"region": "ap-south-1"},
            '● DescribeInstances [aws-compute]("ap-south-1")',
        ),
    ],
)
def test_format_tool_display_legacy_output_unchanged(name, args, expected) -> None:
    assert format_tool_display(name, args) == expected


def test_format_tool_parts_splits_name_scope_detail() -> None:
    p = format_tool_parts(
        "plugin__aws-compute_talkops__describe_instances_f0b5243c096e",
        {"region": "ap-south-1"},
    )
    assert (p.name, p.scope, p.detail, p.kind) == (
        "DescribeInstances",
        "aws-compute",
        "ap-south-1",
        "text",
    )
    cmd = format_tool_parts("execute", {"command": "terraform plan"})
    assert (cmd.detail, cmd.kind) == ("terraform plan", "command")


def test_result_summary_pluralisation() -> None:
    assert format_tool_result_summary("read_file", "one") == "⎿ Read 1 line"
    assert format_tool_result_summary("read_file", "a\nb") == "⎿ Read 2 lines"
    assert format_tool_result_summary("grep", "x:1: hit") == "⎿ 1 match found"
    assert format_tool_result_summary("grep", "a\nb") == "⎿ 2 matches found"


# -- ToolCallMessage header ------------------------------------------------


def test_header_running_uses_teal_spinner_and_plain_detail() -> None:
    msg = ToolCallMessage("execute", {"command": "kubectl get pods"})
    msg._status = "running"
    msg._start_time = time.time()
    content = msg._header_content()
    assert content.plain.endswith("Execute  $ kubectl get pods")
    assert "$primary" in _span_styles(content)


def test_header_running_shows_elapsed_after_threshold() -> None:
    msg = ToolCallMessage("execute", {"command": "sleep 60"})
    msg._status = "running"
    msg._start_time = time.time() - 14
    assert msg._header_content().plain.endswith("· 14s")


@pytest.mark.parametrize(
    ("status", "glyph_style"),
    [
        ("success", "bold $success"),
        ("error", "bold $error"),
        ("rejected", "bold $warning"),
        ("pending", "$warning"),
    ],
)
def test_header_glyph_colour_tracks_status(status, glyph_style) -> None:
    msg = ToolCallMessage("read_file", {"file_path": "a.txt"})
    msg._status = status
    content = msg._header_content()
    assert _span_styles(content)[0] == glyph_style
    assert "Read  a.txt" in content.plain


def test_header_error_turns_name_red() -> None:
    msg = ToolCallMessage("execute", {"command": "false"})
    msg._status = "error"
    assert "bold $error" in _span_styles(msg._header_content())[1:]


def test_header_timed_success_shows_duration() -> None:
    msg = ToolCallMessage("execute", {"command": "make"})
    msg._status = "success"
    msg._duration = 4.2
    assert msg._header_content().plain.endswith("· 4s")
    msg._duration = 0.3
    assert "·" not in msg._header_content().plain


def test_header_task_uses_subagent_label() -> None:
    msg = ToolCallMessage("task", {"subagent_type": "aws-security"})
    msg._status = "success"
    assert msg._header_content().plain.endswith("Subagent  aws-security")


# -- de-duplication --------------------------------------------------------


def test_set_error_skips_command_echo_when_header_shows_it() -> None:
    msg = ToolCallMessage("execute", {"command": "terraform plan"})
    msg.set_error("Error: boom")
    assert msg._output == "Error: boom"


def test_set_error_keeps_command_echo_for_multiline_command() -> None:
    command = "set -e\nterraform plan"
    msg = ToolCallMessage("execute", {"command": command})
    msg.set_error("Error: boom")
    assert msg._output == f"$ {command}\n\nError: boom"


def test_arg_shown_in_header() -> None:
    assert _arg_shown_in_header("ap-south-1", "ap-south-1")
    long = "x" * 60
    assert _arg_shown_in_header(long, "x" * 40 + "…")
    assert not _arg_shown_in_header("us-east-1", "ap-south-1")
    assert not _arg_shown_in_header(["a"], "1 items")


# -- group summary ---------------------------------------------------------


def test_emphasize_counts_highlights_numbers_only() -> None:
    pieces = _emphasize_counts("Read 2 files, ran 10 commands")
    assert ("2", "$primary") in pieces
    assert ("10", "$primary") in pieces
    assert "".join(p if isinstance(p, str) else p[0] for p in pieces) == (
        "Read 2 files, ran 10 commands"
    )


def test_live_line_highlights_in_flight_segment() -> None:
    content = ToolGroupSummary._live_line("⠋", ["read_file"], ["execute"])
    assert content.plain == "⠋ Read 1 file, running 1 shell command…"
    active = [
        content.plain[s.start : s.end]
        for s in content.spans
        if str(s.style) == "$primary"
    ]
    assert "running 1 shell command…" in active
