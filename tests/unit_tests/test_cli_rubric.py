"""Unit tests for rubric CLI resolution, parser arguments, and flag validation."""

from pathlib import Path
import tempfile
import pytest
from opscloud.cli.main import _resolve_rubric_text, build_parser, main
from opscloud.state.goal_state_limits import RUBRIC_CHAR_LIMIT, GoalStateSizeError


def test_resolve_rubric_text_none():
    assert _resolve_rubric_text(None) is None


def test_resolve_rubric_text_literal():
    text = "- Terraform plan succeeded\n- KMS key is enabled"
    assert _resolve_rubric_text(text) == text


def test_resolve_rubric_text_whitespace():
    with pytest.raises(ValueError, match="must not be empty"):
        _resolve_rubric_text("   \n\t  ")


def test_resolve_rubric_text_at_path():
    with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as f:
        f.write("- CloudWatch alarms are created\n- SNS subscription confirmed")
        f.flush()
        file_path = f.name

    try:
        resolved = _resolve_rubric_text(f"@{file_path}")
        assert resolved is not None
        assert "- CloudWatch alarms are created" in resolved
    finally:
        Path(file_path).unlink(missing_ok=True)


def test_resolve_rubric_text_missing_file():
    with pytest.raises(ValueError, match="Could not read rubric file"):
        _resolve_rubric_text("@/tmp/nonexistent_rubric_file_xyz_123.md")


def test_resolve_rubric_text_empty_file():
    with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as f:
        f.write("   \n")
        f.flush()
        file_path = f.name

    try:
        with pytest.raises(ValueError, match="is empty"):
            _resolve_rubric_text(f"@{file_path}")
    finally:
        Path(file_path).unlink(missing_ok=True)


def test_resolve_rubric_text_oversized():
    oversized = "x" * (RUBRIC_CHAR_LIMIT + 1)
    with pytest.raises(GoalStateSizeError):
        _resolve_rubric_text(oversized)


def test_parser_rubric_arguments():
    parser = build_parser()
    args = parser.parse_args([
        "-p", "Migrate DynamoDB table",
        "--rubric", "- Point in time recovery is enabled",
        "--rubric-model", "anthropic:claude-3-5-haiku",
        "--rubric-max-iterations", "5",
    ])
    assert args.prompt == "Migrate DynamoDB table"
    assert args.rubric == "- Point in time recovery is enabled"
    assert args.rubric_model == "anthropic:claude-3-5-haiku"
    assert args.rubric_max_iterations == 5


def test_main_conflicting_goal_and_rubric(capsys):
    ret = main([
        "--goal", "Interactive goal",
        "--rubric", "Headless rubric",
    ])
    assert ret == 1
    captured = capsys.readouterr()
    assert "Cannot specify both --goal and --rubric" in captured.err


def test_main_rubric_without_prompt(capsys):
    ret = main([
        "--rubric", "Headless rubric",
    ])
    assert ret == 1
    captured = capsys.readouterr()
    assert "--rubric requires -p/--prompt" in captured.err
