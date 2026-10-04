"""Unit tests for AskUserMiddleware, question validation, answer parsing, and cancellation handling."""

from unittest.mock import MagicMock
import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import SystemMessage, ToolMessage
from langgraph.types import Command

from opscloud.middleware.ask_user import (
    ASK_USER_SYSTEM_PROMPT,
    AskUserMiddleware,
    _parse_answers,
    _validate_questions,
)
from opscloud.schema.interrupts import Question


def test_validate_questions_valid():
    questions: list[Question] = [
        {"question": "What is the cluster name?", "type": "text"},
        {
            "question": "Which environment?",
            "type": "multiple_choice",
            "choices": [
                {"label": "Production", "value": "prod"},
                {"label": "Staging", "value": "stage"},
            ],
        },
        {
            "question": "Which namespaces to inspect?",
            "type": "multi_select",
            "choices": [
                {"label": "kube-system", "value": "kube-system"},
                {"label": "default", "value": "default"},
            ],
        },
    ]
    # Should not raise
    _validate_questions(questions)


def test_validate_questions_empty_list():
    with pytest.raises(ValueError, match="at least one question"):
        _validate_questions([])


def test_validate_questions_empty_text():
    with pytest.raises(ValueError, match="non-empty 'question' text"):
        _validate_questions([{"question": "   ", "type": "text"}])


def test_validate_questions_unsupported_type():
    with pytest.raises(ValueError, match="unsupported ask_user question type"):
        _validate_questions([{"question": "How?", "type": "invalid_type"}])  # type: ignore[typeddict-item]


def test_validate_questions_missing_choices():
    with pytest.raises(ValueError, match="requires non-empty 'choices'"):
        _validate_questions([{"question": "Which?", "type": "multiple_choice", "choices": []}])


def test_parse_answers_success():
    questions: list[Question] = [
        {"question": "Cluster?", "type": "text"},
        {"question": "Region?", "type": "text"},
    ]
    response = {"answers": ["eks-prod", "us-east-1"]}
    cmd = _parse_answers(response, questions, "call_999")

    assert isinstance(cmd, Command)
    messages = cmd.update.get("messages", [])
    assert len(messages) == 1
    assert isinstance(messages[0], ToolMessage)
    assert messages[0].tool_call_id == "call_999"
    assert "eks-prod" in messages[0].content
    assert "us-east-1" in messages[0].content


def test_parse_answers_cancelled():
    questions: list[Question] = [
        {"question": "Confirm deletion?", "type": "text"},
    ]
    response = {"status": "cancelled"}
    cmd = _parse_answers(response, questions, "call_cancel")

    messages = cmd.update.get("messages", [])
    assert len(messages) == 1
    assert "(cancelled by user)" in messages[0].content


def test_parse_answers_error():
    questions: list[Question] = [
        {"question": "Select target?", "type": "text"},
    ]
    response = {"status": "error", "error": "User prompt timed out"}
    cmd = _parse_answers(response, questions, "call_err")

    messages = cmd.update.get("messages", [])
    assert "(error: User prompt timed out)" in messages[0].content


def test_parse_answers_padded():
    questions: list[Question] = [
        {"question": "Q1", "type": "text"},
        {"question": "Q2", "type": "text"},
    ]
    # Only 1 answer provided
    response = {"answers": ["Ans1"]}
    cmd = _parse_answers(response, questions, "call_pad")

    messages = cmd.update.get("messages", [])
    assert "Ans1" in messages[0].content
    assert "(no answer)" in messages[0].content


def test_ask_user_middleware_tools_and_wrapping():
    mw = AskUserMiddleware()
    assert len(mw.tools) == 1
    assert mw.tools[0].name == "ask_user"

    request = ModelRequest(
        model=MagicMock(),
        system_message=SystemMessage(content="Base system prompt."),
        messages=[],
        state={},
        runtime=None,
    )

    passed_request = None

    def handler(req):
        nonlocal passed_request
        passed_request = req
        return "response"

    res = mw.wrap_model_call(request, handler)
    assert res == "response"
    assert passed_request is not None
    assert "Base system prompt." in passed_request.system_message.content
    assert ASK_USER_SYSTEM_PROMPT in passed_request.system_message.content


@pytest.mark.asyncio
async def test_ask_user_middleware_awrap_model_call():
    mw = AskUserMiddleware()

    request = ModelRequest(
        model=MagicMock(),
        system_message=SystemMessage(content="Async base prompt."),
        messages=[],
        state={},
        runtime=None,
    )

    async def async_handler(req):
        return req.system_message.content

    content = await mw.awrap_model_call(request, async_handler)
    assert "Async base prompt." in content
    assert ASK_USER_SYSTEM_PROMPT in content
