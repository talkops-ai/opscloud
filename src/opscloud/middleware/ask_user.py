"""Ask User middleware for interactive clarification during agent execution."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse
from langchain.tools import InjectedToolCallId
from langchain_core.messages import SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.types import Command, interrupt

from opscloud.middleware.registry import register_middleware
from opscloud.schema.interrupts import (
    CHOICE_QUESTION_TYPES,
    QUESTION_TYPES,
    AskUserRequest,
    Choice,
    Question,
    format_ask_user_transcript,
)
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

ASK_USER_TOOL_DESCRIPTION = """Ask the user one or more questions when you need clarification or input before proceeding with cloud operations.

Each question can be either:
- "text": Free-form text response from the user
- "multiple_choice": User selects exactly one option from predefined choices
- "multi_select": User selects one or more options from predefined choices

Use this tool when:
- You need clarification on cloud resource names, VPC IDs, region selection, or environments
- You want the user to choose between multiple valid operational approaches or resources
- You need specific confirmation or choices only the user can provide
"""

ASK_USER_SYSTEM_PROMPT = """## `ask_user`

You have access to the `ask_user` tool to ask the user questions when you need clarification or confirmation.
Use this tool sparingly - only when you genuinely need information from the user that you cannot determine from context or CLI commands.
"""


def _validate_questions(questions: list[Question]) -> None:
    if not questions:
        raise ValueError("ask_user requires at least one question")
    for q in questions:
        question_text = q.get("question")
        if not isinstance(question_text, str) or not question_text.strip():
            raise ValueError("ask_user questions must have non-empty 'question' text")
        question_type = q.get("type")
        if question_type not in QUESTION_TYPES:
            raise ValueError(f"unsupported ask_user question type: {question_type!r}")
        if question_type in CHOICE_QUESTION_TYPES and not q.get("choices"):
            raise ValueError(f"{question_type} question {q.get('question')!r} requires non-empty 'choices'")


def _parse_answers(
    response: object,
    questions: list[Question],
    tool_call_id: str,
) -> Command[Any]:
    answers: list[str] = []
    if isinstance(response, dict):
        status = response.get("status")
        if status == "cancelled":
            answers = ["(cancelled by user)" for _ in questions]
        elif status == "error":
            err_msg = response.get("error", "unknown error")
            answers = [f"(error: {err_msg})" for _ in questions]
        else:
            raw_answers = response.get("answers", [])
            if isinstance(raw_answers, list):
                answers = [str(a) for a in raw_answers]
    elif isinstance(response, (list, tuple)):
        answers = [str(a) for a in response]

    # Pad missing answers if fewer provided
    if len(answers) < len(questions):
        answers.extend(["(no answer)"] * (len(questions) - len(answers)))

    result_text = format_ask_user_transcript(questions, answers)
    return Command(
        update={
            "messages": [ToolMessage(result_text, tool_call_id=tool_call_id)],
        }
    )


@register_middleware(name="ask_user")
class AskUserMiddleware(AgentMiddleware[Any, Any]):
    """Expose ask_user tool and inject system guidance into model requests."""

    def __init__(
        self,
        *,
        system_prompt: str = ASK_USER_SYSTEM_PROMPT,
        tool_description: str = ASK_USER_TOOL_DESCRIPTION,
    ) -> None:
        super().__init__()
        self.system_prompt = system_prompt
        self.tool_description = tool_description

        @tool(description=self.tool_description)
        def _ask_user(
            questions: list[Question],
            tool_call_id: Annotated[str, InjectedToolCallId],
        ) -> Command[Any]:
            _validate_questions(questions)
            ask_request = AskUserRequest(
                type="ask_user",
                questions=questions,
                tool_call_id=tool_call_id,
            )
            response = interrupt(ask_request)
            return _parse_answers(response, questions, tool_call_id)

        _ask_user.name = "ask_user"
        self.tools = [_ask_user]

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        prompt = self.system_prompt
        new_prompt = f"{request.system_prompt or ''}\n\n{prompt}"
        updated_request = request.override(system_message=SystemMessage(content=new_prompt))
        return handler(updated_request)

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        prompt = self.system_prompt
        new_prompt = f"{request.system_prompt or ''}\n\n{prompt}"
        updated_request = request.override(system_message=SystemMessage(content=new_prompt))
        return await handler(updated_request)


__all__ = [
    "ASK_USER_SYSTEM_PROMPT",
    "ASK_USER_TOOL_DESCRIPTION",
    "AskUserMiddleware",
    "AskUserRequest",
    "Choice",
    "Question",
]
