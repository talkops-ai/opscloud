"""Lightweight types for the ask-user interrupt protocol.

Re-exports canonical schemas from `opscloud.schema.interrupts` so `textual_adapter`
and widgets can reference the types without pulling in the langchain middleware stack.
"""

from __future__ import annotations

from typing import Literal
from typing_extensions import TypedDict

from opscloud.schema.interrupts import (
    ASK_USER_ANSWERED_SUMMARY,
    ASK_USER_CANCELLED_ANSWER,
    ASK_USER_CANCELLED_SUMMARY,
    ASK_USER_ERROR_ANSWER_PREFIX,
    ASK_USER_FAILED_SUMMARY,
    ASK_USER_NOTHING_SELECTED,
    ASK_USER_NO_ANSWER,
    AskUserAnswered,
    AskUserCancelled,
    AskUserRequest,
    AskUserResumePayload,
    AskUserWidgetResult,
    CHOICE_QUESTION_TYPES,
    Choice,
    QUESTION_TYPES,
    Question,
    QuestionType,
    ValidatedQuestion,
    ask_user_answer_is_empty,
    decode_multi_select_answer,
    encode_multi_select_answer,
    format_ask_user_error_answer,
    format_ask_user_transcript,
    render_ask_user_transcript_for_display,
)

ASK_USER_AUTHORIZATION_METADATA_KEY = "opscloud_ask_user_authorization"
MAX_ASK_USER_AUTHORIZATION_ANSWER_CHARS = 4000


class AskUserAuthorizationReceipt(TypedDict):
    """Trusted same-turn authorization recorded after an ask_user response."""

    version: Literal[1]
    thread_id: str
    turn_id: str
    tool_call_id: str
    answers: list[str]


__all__ = [
    "ASK_USER_ANSWERED_SUMMARY",
    "ASK_USER_AUTHORIZATION_METADATA_KEY",
    "ASK_USER_CANCELLED_ANSWER",
    "ASK_USER_CANCELLED_SUMMARY",
    "ASK_USER_ERROR_ANSWER_PREFIX",
    "ASK_USER_FAILED_SUMMARY",
    "ASK_USER_NOTHING_SELECTED",
    "ASK_USER_NO_ANSWER",
    "AskUserAnswered",
    "AskUserAuthorizationReceipt",
    "AskUserCancelled",
    "AskUserRequest",
    "AskUserResumePayload",
    "AskUserWidgetResult",
    "CHOICE_QUESTION_TYPES",
    "Choice",
    "MAX_ASK_USER_AUTHORIZATION_ANSWER_CHARS",
    "QUESTION_TYPES",
    "Question",
    "QuestionType",
    "ValidatedQuestion",
    "ask_user_answer_is_empty",
    "decode_multi_select_answer",
    "encode_multi_select_answer",
    "format_ask_user_error_answer",
    "format_ask_user_transcript",
    "render_ask_user_transcript_for_display",
]
