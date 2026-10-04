"""Unit tests for ReliableRubricMiddleware, transport retry, and stream events."""

from unittest.mock import MagicMock
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from opscloud.middleware.goal_state_notice import build_goal_state_notice
from opscloud.middleware.reliable_rubric import (
    ReliableRubricMiddleware,
    _is_transient_grader_transport_error,
    _without_internal_control_messages,
)
from opscloud.rubrics.evaluator import _validate_rubric_grader_read_path


def test_validate_rubric_grader_read_path():
    assert _validate_rubric_grader_read_path("../traversal.txt") is not None
    assert _validate_rubric_grader_read_path("/large_tool_results/result1.txt") is None
    assert _validate_rubric_grader_read_path("valid/path.tf", repository_root=".") is None


def test_is_transient_grader_transport_error():
    assert _is_transient_grader_transport_error(ConnectionResetError("peer closed")) is True
    assert _is_transient_grader_transport_error(TimeoutError("request timed out")) is True
    assert _is_transient_grader_transport_error(Exception("429 Too Many Requests")) is True
    assert _is_transient_grader_transport_error(Exception("503 Service Unavailable")) is True
    assert _is_transient_grader_transport_error(ValueError("Invalid argument")) is False
    assert _is_transient_grader_transport_error(TypeError("Type mismatch")) is False


def test_without_internal_control_messages():
    control_msg = build_goal_state_notice({
        "goal": "Verify IAM policies",
        "rubric_criteria": "- Ensure least privilege",
    })
    human_msg = HumanMessage(content="Please check AWS configuration")
    ai_msg = AIMessage(content="Checking configuration now...")
    tool_msg = ToolMessage(content="aws sts get-caller-identity returned ok", tool_call_id="call_1")

    state = {
        "messages": [human_msg, control_msg, ai_msg, tool_msg],
        "rubric": "- Ensure least privilege",
    }

    filtered = _without_internal_control_messages(state)
    filtered_msgs = filtered["messages"]

    assert len(filtered_msgs) == 3
    assert human_msg in filtered_msgs
    assert ai_msg in filtered_msgs
    assert tool_msg in filtered_msgs
    assert control_msg not in filtered_msgs


def test_reliable_rubric_stream_event_emission():
    middleware = ReliableRubricMiddleware(
        model="anthropic:claude-3-5-haiku-20241022",
        max_iterations=2,
    )
    events_emitted = []

    mock_runtime = MagicMock()
    mock_runtime.stream_writer = lambda evt: events_emitted.append(evt)

    middleware._emit_stream_event(mock_runtime, {"type": "rubric_evaluation_start", "iteration": 0})
    assert len(events_emitted) == 1
    assert events_emitted[0]["type"] == "rubric_evaluation_start"
    assert events_emitted[0]["iteration"] == 0


def test_reliable_rubric_grader_input_signature_and_correction():
    middleware = ReliableRubricMiddleware(
        model="anthropic:claude-3-5-haiku-20241022",
        max_iterations=2,
    )
    state = {
        "messages": [HumanMessage(content="test")],
        "rubric": "1. Must pass lint",
        "_current_grading_run_id": "run-42",
    }
    # Test without correction
    inp1 = middleware._grader_input(state, 0)
    assert "messages" in inp1
    assert inp1["rubric_grading_operation_id"] == "run-42:0"
    content1 = inp1["messages"][0].content
    assert "This is grader iteration 0." in content1

    # Test with correction (regrading after unusable response)
    inp2 = middleware._grader_input(state, 1, correction="Return all criteria.")
    assert inp2["rubric_grading_operation_id"] == "run-42:1"
    content2 = inp2["messages"][0].content
    assert "This is grader iteration 1, regrading after an unusable response. Return all criteria." in content2
