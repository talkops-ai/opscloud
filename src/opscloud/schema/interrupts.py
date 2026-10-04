"""Typed interrupt and resume payload schemas for OpsCloud.

Provides deterministic, typed Pydantic models and TypedDicts for interrupt
requests and resumptions, aligned with official dcode contracts and terminal-native
(CLI and Textual TUI) operations.

Covers:
1. Tool Approvals (HITL): HITLRequest, ActionRequest, ReviewConfig, HitlDecision,
   ApproveDecision, RejectDecision, EditDecision, SwitchManualDecision, AutoApproveAllDecision, HitlResumePayload.
2. Interactive Question Prompts: AskUserRequest, Question, Choice, ValidatedQuestion,
   AskUserAnswered, AskUserCancelled, AskUserResumePayload, multi-select encoders/decoders,
   and transcript rendering.
3. Goal & Acceptance Criteria Review: GoalReviewRequest, GoalReviewResumePayload.
4. Hook Invocation Interrupts: HookInvocationInterrupt, HookInvocationRequest, HookInvocationResponse.
"""

from __future__ import annotations

import json
from typing import (
    Annotated,
    Any,
    Literal,
    NotRequired,
    Sequence,
    assert_never,
    get_args,
)

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
)
from typing_extensions import TypedDict

from opscloud.hooks.interrupt import (
    HOOK_INVOCATION_INTERRUPT_TYPE,
    HookInvocationInterrupt,
    build_hook_interrupt_payload,
    build_hook_resume_value,
    is_hook_interrupt_payload,
    parse_hook_interrupt_payload,
    parse_hook_resume_value,
)
from opscloud.hooks.models.transport import (
    HookInvocationRequest,
    HookInvocationResponse,
)

# ─────────────────────────────────────────────────────────────────────────────
# 1. Normalization Vocabulary & Decision Tokens
# ─────────────────────────────────────────────────────────────────────────────

_CONFIRM_DECISIONS: frozenset[str] = frozenset(
    {
        "accept",
        "confirm",
        "accepted",
        "confirmed",
        "approve",
        "approved",
        "approval",
        "proceed",
        "continue",
        "y",
        "yes",
        "ok",
        "true",
        "1",
        "goal_response",
        "hitl_response",
    }
)

_SMART_DECISIONS: frozenset[str] = frozenset(
    {
        "smart_approve_all",
        "enable_smart",
        "smart",
        "s",
    }
)

_AUTO_DECISIONS: frozenset[str] = frozenset(
    {
        "auto_approve_all",
        "enable_auto",
        "auto",
        "a",
        "always",
        "smart_approve_all",
        "enable_smart",
        "smart",
        "s",
    }
)

_EDIT_DECISIONS: frozenset[str] = frozenset(
    {
        "edit",
        "edited",
        "e",
        "modify",
        "modified",
        "update",
        "updated",
    }
)

_SWITCH_MANUAL_DECISIONS: frozenset[str] = frozenset(
    {
        "switch_manual",
        "manual",
        "switch",
        "m",
    }
)

_REJECT_DECISIONS: frozenset[str] = frozenset(
    {
        "reject",
        "rejected",
        "r",
        "deny",
        "denied",
        "disapprove",
        "disapproved",
    }
)

_CANCEL_DECISIONS: frozenset[str] = frozenset(
    {
        "cancel",
        "cancelled",
        "dismiss",
        "dismissed",
        "close",
        "closed",
        "n",
        "no",
    }
)


# ─────────────────────────────────────────────────────────────────────────────
# 2. HITL / Tool Approval Schemas
# ─────────────────────────────────────────────────────────────────────────────

class ActionRequest(TypedDict, total=False):
    """An action request emitted when a tool call requires human review."""

    name: str
    args: dict[str, Any]
    id: str


class ReviewConfig(TypedDict, total=False):
    """Configuration governing review options for a specific action."""

    action_name: str
    allowed_decisions: list[str]


class HITLRequest(TypedDict, total=False):
    """LangGraph interrupt payload when sensitive tools require approval."""

    action_requests: list[ActionRequest]
    review_configs: list[ReviewConfig]


class HitlDecision(BaseModel):
    """Individual tool approval decision with support for args editing and manual switch."""

    model_config = ConfigDict(extra="ignore")

    type: Literal["approve", "reject", "edit", "switch_manual", "auto_approve_all", "smart_approve_all"] = "approve"
    message: str | None = None
    args: dict[str, Any] | None = None

    def to_command_dict(self) -> dict[str, Any]:
        """Convert to dictionary matching LangGraph resume expectation."""
        res: dict[str, Any] = {"type": self.type}
        if self.message is not None:
            res["message"] = self.message
        if self.args is not None:
            res["args"] = self.args
        return res


class ApproveDecision(BaseModel):
    """Explicit approve decision."""

    type: Literal["approve"] = "approve"


class RejectDecision(BaseModel):
    """Explicit reject decision with optional reason."""

    type: Literal["reject"] = "reject"
    message: str | None = None


class EditDecision(BaseModel):
    """Explicit edit decision with revised tool arguments."""

    type: Literal["edit"] = "edit"
    args: dict[str, Any] = Field(default_factory=dict)


class SwitchManualDecision(BaseModel):
    """Switch from auto mode fallback to manual review."""

    type: Literal["switch_manual"] = "switch_manual"


class AutoApproveAllDecision(BaseModel):
    """Enable auto-approval for all tools in session."""

    type: Literal["auto_approve_all"] = "auto_approve_all"


class SmartApproveAllDecision(BaseModel):
    """Enable smart-approval (Jev protected) for all tools in session."""

    type: Literal["smart_approve_all"] = "smart_approve_all"


class HitlResumePayload(BaseModel):
    """Resume payload for HITL tool approval interrupts."""

    decisions: list[HitlDecision] = Field(default_factory=list)
    auto_approve_requested: bool = False
    smart_approve_requested: bool = False

    def to_command_value(self) -> dict[str, Any]:
        """Serialize for LangGraph `Command(resume=...)`."""
        return {
            "decisions": [d.to_command_dict() for d in self.decisions]
        }

    @classmethod
    def from_raw(
        cls,
        data: Any,
        count: int = 1,
    ) -> HitlResumePayload:
        """Parse arbitrary client data (dict, list, string) into HitlResumePayload.

        Normalizes terminal CLI options, TUI modals, and headless responses.
        """
        if isinstance(data, cls):
            return data

        parsed = data
        if isinstance(data, str) and data.strip():
            trimmed = data.strip()
            if (trimmed.startswith("{") and trimmed.endswith("}")) or (
                trimmed.startswith("[") and trimmed.endswith("]")
            ):
                try:
                    parsed = json.loads(trimmed)
                except (json.JSONDecodeError, ValueError):
                    parsed = data

        auto_mode = False
        smart_mode = False
        target_count = max(1, count)

        if isinstance(parsed, dict):
            # Check for decisions array from HITL response
            decisions_raw = parsed.get("decisions")
            if isinstance(decisions_raw, list) and decisions_raw:
                decisions: list[HitlDecision] = []
                for item in decisions_raw:
                    if isinstance(item, dict):
                        raw_t = (
                            str(
                                item.get("type") or item.get("decision") or "approve",
                            )
                            .lower()
                            .strip()
                        )
                        msg = item.get("message") or item.get("feedback")
                        edited_args = item.get("args") or item.get("edited_args") or item.get("parameters")

                        if raw_t in _SMART_DECISIONS:
                            smart_mode = True
                            auto_mode = True
                            dec_type: Literal["approve", "reject", "edit", "switch_manual", "auto_approve_all", "smart_approve_all"] = "approve"
                        elif raw_t in _AUTO_DECISIONS:
                            auto_mode = True
                            dec_type = "approve"
                        elif raw_t in _EDIT_DECISIONS:
                            dec_type = "edit"
                        elif raw_t in _SWITCH_MANUAL_DECISIONS:
                            dec_type = "switch_manual"
                        elif raw_t in _CONFIRM_DECISIONS:
                            dec_type = "approve"
                        else:
                            dec_type = "reject"

                        decisions.append(
                            HitlDecision(
                                type=dec_type,
                                message=str(msg) if msg else None,
                                args=edited_args if isinstance(edited_args, dict) else None,
                            ),
                        )
                    else:
                        s = str(item).lower().strip()
                        if s in _SMART_DECISIONS:
                            smart_mode = True
                            auto_mode = True
                            dec_type = "approve"
                        elif s in _AUTO_DECISIONS:
                            auto_mode = True
                            dec_type = "approve"
                        elif s in _EDIT_DECISIONS:
                            dec_type = "edit"
                        elif s in _SWITCH_MANUAL_DECISIONS:
                            dec_type = "switch_manual"
                        elif s in _CONFIRM_DECISIONS:
                            dec_type = "approve"
                        else:
                            dec_type = "reject"
                        decisions.append(HitlDecision(type=dec_type))

                if decisions:
                    return cls(
                        decisions=decisions,
                        auto_approve_requested=auto_mode,
                        smart_approve_requested=smart_mode,
                    )

            # Check single decision / action
            raw_dec = (
                parsed.get("decision")
                or parsed.get("action")
                or parsed.get("choice")
                or parsed.get("type")
                or parsed.get("status")
            )
            dec_str = str(raw_dec).lower().strip() if raw_dec is not None else ""
            msg = parsed.get("message") or parsed.get("feedback") or parsed.get("rejectionReason")
            msg_str = str(msg).strip() if msg else None
            edited_args = parsed.get("args") or parsed.get("edited_args") or parsed.get("parameters")

            if dec_str in _SMART_DECISIONS:
                return cls(
                    decisions=[HitlDecision(type="approve") for _ in range(target_count)],
                    auto_approve_requested=True,
                    smart_approve_requested=True,
                )
            if dec_str in _AUTO_DECISIONS:
                return cls(
                    decisions=[HitlDecision(type="approve") for _ in range(target_count)],
                    auto_approve_requested=True,
                    smart_approve_requested=False,
                )
            if dec_str in _EDIT_DECISIONS:
                return cls(
                    decisions=[
                        HitlDecision(
                            type="edit",
                            message=msg_str,
                            args=edited_args if isinstance(edited_args, dict) else None,
                        )
                        for _ in range(target_count)
                    ],
                    auto_approve_requested=False,
                    smart_approve_requested=False,
                )
            if dec_str in _SWITCH_MANUAL_DECISIONS:
                return cls(
                    decisions=[HitlDecision(type="switch_manual") for _ in range(target_count)],
                    auto_approve_requested=False,
                    smart_approve_requested=False,
                )
            if dec_str in _CONFIRM_DECISIONS:
                return cls(
                    decisions=[HitlDecision(type="approve") for _ in range(target_count)],
                    auto_approve_requested=False,
                    smart_approve_requested=False,
                )
            if dec_str in _REJECT_DECISIONS or dec_str in _CANCEL_DECISIONS:
                return cls(
                    decisions=[HitlDecision(type="reject", message=msg_str) for _ in range(target_count)],
                    auto_approve_requested=False,
                    smart_approve_requested=False,
                )
            return cls(
                decisions=[HitlDecision(type="approve") for _ in range(target_count)],
                auto_approve_requested=False,
                smart_approve_requested=False,
            )

        if isinstance(parsed, list):
            decisions = []
            for item in parsed:
                s = str(item).lower().strip()
                if s in _SMART_DECISIONS:
                    smart_mode = True
                    auto_mode = True
                    dec_type = "approve"
                elif s in _AUTO_DECISIONS:
                    auto_mode = True
                    dec_type = "approve"
                elif s in _EDIT_DECISIONS:
                    dec_type = "edit"
                elif s in _SWITCH_MANUAL_DECISIONS:
                    dec_type = "switch_manual"
                elif s in _CONFIRM_DECISIONS:
                    dec_type = "approve"
                else:
                    dec_type = "reject"
                decisions.append(HitlDecision(type=dec_type))
            return cls(
                decisions=decisions or [HitlDecision(type="approve")],
                auto_approve_requested=auto_mode,
                smart_approve_requested=smart_mode,
            )

        if isinstance(parsed, str):
            s = parsed.lower().strip()
            if s in _SMART_DECISIONS:
                return cls(
                    decisions=[HitlDecision(type="approve") for _ in range(target_count)],
                    auto_approve_requested=True,
                    smart_approve_requested=True,
                )
            if s in _AUTO_DECISIONS:
                return cls(
                    decisions=[HitlDecision(type="approve") for _ in range(target_count)],
                    auto_approve_requested=True,
                    smart_approve_requested=False,
                )
            if s in _EDIT_DECISIONS:
                return cls(
                    decisions=[HitlDecision(type="edit") for _ in range(target_count)],
                    auto_approve_requested=False,
                    smart_approve_requested=False,
                )
            if s in _SWITCH_MANUAL_DECISIONS:
                return cls(
                    decisions=[HitlDecision(type="switch_manual") for _ in range(target_count)],
                    auto_approve_requested=False,
                    smart_approve_requested=False,
                )
            if s in _CONFIRM_DECISIONS:
                return cls(
                    decisions=[HitlDecision(type="approve") for _ in range(target_count)],
                    auto_approve_requested=False,
                    smart_approve_requested=False,
                )
            if s in _REJECT_DECISIONS or s in _CANCEL_DECISIONS:
                return cls(
                    decisions=[HitlDecision(type="reject") for _ in range(target_count)],
                    auto_approve_requested=False,
                    smart_approve_requested=False,
                )
            return cls(
                decisions=[HitlDecision(type="approve") for _ in range(target_count)],
                auto_approve_requested=False,
                smart_approve_requested=False,
            )

        return cls(
            decisions=[HitlDecision(type="approve") for _ in range(target_count)],
            auto_approve_requested=False,
            smart_approve_requested=False,
        )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Interactive Question Prompts (ask_user) Schemas & Encoders
# ─────────────────────────────────────────────────────────────────────────────

QuestionType = Literal["text", "multiple_choice", "multi_select"]
"""Supported `ask_user` question types."""

QUESTION_TYPES: frozenset[str] = frozenset(get_args(QuestionType))


def _requires_choices(question_type: QuestionType) -> bool:
    """Return whether question_type needs a non-empty choices list."""
    if question_type == "text":
        return False
    if question_type == "multiple_choice" or question_type == "multi_select":
        return True
    assert_never(question_type)


CHOICE_QUESTION_TYPES: frozenset[str] = frozenset(
    qt for qt in get_args(QuestionType) if _requires_choices(qt)
)


def encode_multi_select_answer(values: list[str]) -> str:
    """Encode selected values of a multi_select answer into a JSON string.

    Keeps exactly one slot per question on the wire.
    """
    return json.dumps(values, ensure_ascii=False)


def decode_multi_select_answer(raw: str) -> list[str] | None:
    """Decode a multi_select answer produced by encode_multi_select_answer."""
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, RecursionError):
        return None
    if not isinstance(decoded, list) or not all(isinstance(v, str) for v in decoded):
        return None
    return decoded


def ask_user_answer_is_empty(answer: str, question_type: object) -> bool:
    """Return whether an ask_user answer counts as empty/unanswered."""
    if question_type == "multi_select":
        return not decode_multi_select_answer(answer)
    return not answer.strip()


def _validate_question_text(text: str) -> str:
    if not text.strip():
        raise ValueError("question text must not be blank")
    return text


class Choice(TypedDict):
    """A single choice option for a multiple choice or multi-select question."""

    value: Annotated[str, Field(description="The display label for this choice.")]


def _validate_choice(choice: Choice) -> Choice:
    if not choice.get("value", "").strip():
        raise ValueError(f"choice has a blank 'value': {choice!r}")
    return choice


def _validate_question(question: Question) -> Question:
    q_type = question.get("type", "text")
    q_text = question.get("question", "")
    choices = question.get("choices")
    if q_type in CHOICE_QUESTION_TYPES:
        if not choices:
            raise ValueError(f"{q_type} question {q_text!r} requires a non-empty 'choices' list")
    elif choices:
        raise ValueError(f"{q_type} question {q_text!r} must not define 'choices'")
    return question


class Question(TypedDict):
    """A question to ask the user in terminal CLI or TUI."""

    question: Annotated[
        str,
        AfterValidator(_validate_question_text),
        Field(description="The question text to display.", min_length=1),
    ]
    type: Annotated[
        QuestionType,
        Field(
            description=(
                "Question type. 'text' for free-form input, 'multiple_choice' for "
                "picking exactly one option, 'multi_select' for picking one or more options."
            )
        ),
    ]
    choices: NotRequired[
        Annotated[
            list[Annotated[Choice, AfterValidator(_validate_choice)]],
            Field(description="Options for multiple_choice and multi_select questions."),
        ]
    ]
    required: NotRequired[
        Annotated[
            bool,
            Field(description="Whether the user must answer. Defaults to true if omitted."),
        ]
    ]


ValidatedQuestion = Annotated[Question, AfterValidator(_validate_question)]


def _validate_questions(questions: list[ValidatedQuestion]) -> list[ValidatedQuestion]:
    if not questions:
        raise ValueError("ask_user requires at least one question")
    return questions


class AskUserRequest(TypedDict):
    """Request payload sent via interrupt when asking the user questions."""

    type: Literal["ask_user"]
    questions: Annotated[list[ValidatedQuestion], AfterValidator(_validate_questions)]
    tool_call_id: str


class AskUserAnswered(TypedDict):
    """Widget result when the user submits answers."""

    type: Literal["answered"]
    answers: list[str]


class AskUserCancelled(TypedDict):
    """Widget result when the user cancels the prompt."""

    type: Literal["cancelled"]


AskUserWidgetResult = AskUserAnswered | AskUserCancelled


ASK_USER_NOTHING_SELECTED = "(nothing selected)"
ASK_USER_NO_ANSWER = "(no answer)"
ASK_USER_CANCELLED_ANSWER = "(cancelled)"
ASK_USER_ERROR_ANSWER_PREFIX = "(error: "
ASK_USER_ANSWERED_SUMMARY = "User answered"
ASK_USER_CANCELLED_SUMMARY = "Question cancelled"
ASK_USER_FAILED_SUMMARY = "Question failed"


def format_ask_user_error_answer(detail: str) -> str:
    """Render the placeholder answer recorded for every question on failure."""
    return f"{ASK_USER_ERROR_ANSWER_PREFIX}{detail})"


def format_ask_user_transcript(
    questions: Sequence[Question | dict[str, Any]],
    answers: Sequence[str],
) -> str:
    """Render questions and answers as blank-line separated Q:/A: blocks."""
    blocks = [
        f"Q: {q.get('question', '')}\n"
        f"A: {answers[i] if i < len(answers) else ASK_USER_NO_ANSWER}"
        for i, q in enumerate(questions)
    ]
    return "\n\n".join(blocks)


def render_ask_user_transcript_for_display(
    questions: list[Question | dict[str, Any]],
    transcript: str,
) -> str | None:
    """Re-render transcript with multi_select answers unpacked into human-readable lines."""
    if not questions:
        return None
    anchors = [f"Q: {q.get('question', '')}\nA: " for q in questions]
    answers: list[str] = []
    position = 0
    for index, anchor in enumerate(anchors):
        if not transcript.startswith(anchor, position):
            return None
        position += len(anchor)
        if index + 1 == len(anchors):
            answers.append(transcript[position:])
            break
        separator = f"\n\n{anchors[index + 1]}"
        end = transcript.find(separator, position)
        if end == -1 or transcript.find(separator, end + 2) != -1:
            return None
        answers.append(transcript[position:end])
        position = end + 2

    changed = False
    blocks: list[str] = []
    for q, ans in zip(questions, answers, strict=True):
        rendered = ans
        if q.get("type") == "multi_select":
            values = decode_multi_select_answer(ans)
            if values is not None:
                rendered = "\n".join(values) if values else ASK_USER_NOTHING_SELECTED
                changed = changed or rendered != ans
        blocks.append(f"Q: {q.get('question', '')}\nA: {rendered}")
    if not changed:
        return None
    return "\n\n".join(blocks)


class AskUserResumePayload(BaseModel):
    """Resume payload for ask_user question prompts."""

    status: Literal["answered", "cancelled", "error"] = "answered"
    answers: list[str] = Field(default_factory=list)
    error: str | None = None

    def to_command_value(self) -> dict[str, Any]:
        """Serialize for LangGraph `Command(resume=...)`."""
        res: dict[str, Any] = {
            "status": self.status,
            "answers": self.answers,
        }
        if self.error:
            res["error"] = self.error
        return res

    @classmethod
    def from_raw(
        cls,
        data: Any,
        questions: list[Any] | None = None,
    ) -> AskUserResumePayload:
        """Parse arbitrary client data into AskUserResumePayload."""
        if isinstance(data, cls):
            return data

        q_count = len(questions) if questions else 1
        parsed = data
        if isinstance(data, str) and data.strip():
            trimmed = data.strip()
            if (trimmed.startswith("{") and trimmed.endswith("}")) or (
                trimmed.startswith("[") and trimmed.endswith("]")
            ):
                try:
                    parsed = json.loads(trimmed)
                except (json.JSONDecodeError, ValueError):
                    parsed = data

        if isinstance(parsed, dict):
            status = str(parsed.get("status") or parsed.get("type", "answered")).lower().strip()
            err_msg = parsed.get("error")

            if status == "cancelled":
                return cls(
                    status="cancelled",
                    answers=[ASK_USER_CANCELLED_ANSWER for _ in range(q_count)],
                )
            if status == "error":
                return cls(
                    status="error",
                    error=str(err_msg) if err_msg else "unknown error",
                    answers=[format_ask_user_error_answer(str(err_msg or "error")) for _ in range(q_count)],
                )

            if "answers" in parsed:
                raw_ans = parsed["answers"]
                ans_list = [str(x) for x in raw_ans] if isinstance(raw_ans, list) else [str(raw_ans)]
                return cls(status="answered", answers=ans_list)

            if "choice" in parsed:
                return cls(status="answered", answers=[str(parsed["choice"])])
            if "text" in parsed:
                return cls(status="answered", answers=[str(parsed["text"])])
            if "answer" in parsed:
                return cls(status="answered", answers=[str(parsed["answer"])])

            return cls(status="answered", answers=[json.dumps(parsed)])

        if isinstance(parsed, list):
            return cls(status="answered", answers=[str(x) for x in parsed])

        if isinstance(parsed, str):
            s = parsed.strip()
            if s.lower() in ("cancel", "cancelled"):
                return cls(
                    status="cancelled",
                    answers=[ASK_USER_CANCELLED_ANSWER for _ in range(q_count)],
                )
            return cls(status="answered", answers=[s])

        return cls(
            status="answered",
            answers=[str(parsed) if parsed is not None else ""],
        )


# ─────────────────────────────────────────────────────────────────────────────
# 4. Goal, Criteria & Rubric Review Schemas
# ─────────────────────────────────────────────────────────────────────────────

class GoalReviewRequest(TypedDict, total=False):
    """Request payload for human verification of proposed goals and criteria."""

    type: Literal["goal_review"]
    goal: str
    criteria: list[str]
    rubric: str | None


class GoalReviewResumePayload(BaseModel):
    """Resume payload for goal_review / propose_goal interrupts."""

    decision: Literal["confirm", "edit", "reject", "cancel"] = "confirm"
    criteria: list[str] | None = None
    feedback: str | None = None

    def to_command_value(self) -> dict[str, Any]:
        """Serialize for LangGraph `Command(resume=...)`."""
        res: dict[str, Any] = {"decision": self.decision}
        if self.criteria is not None:
            res["criteria"] = self.criteria
        if self.feedback is not None:
            res["feedback"] = self.feedback
        return res

    @classmethod
    def from_raw(
        cls,
        data: Any,
    ) -> GoalReviewResumePayload:
        """Parse arbitrary client data into GoalReviewResumePayload."""
        if isinstance(data, cls):
            return data

        parsed = data
        if isinstance(data, str) and data.strip():
            trimmed = data.strip()
            if (trimmed.startswith("{") and trimmed.endswith("}")) or (
                trimmed.startswith("[") and trimmed.endswith("]")
            ):
                try:
                    parsed = json.loads(trimmed)
                except (json.JSONDecodeError, ValueError):
                    parsed = data

        if isinstance(parsed, dict):
            # Unwrap nested dict if wrapped by interrupt ID
            if len(parsed) == 1:
                key = next(iter(parsed))
                if key not in (
                    "decision",
                    "decisions",
                    "action",
                    "status",
                    "feedback",
                    "criteria",
                    "goalText",
                    "objective",
                    "choice",
                    "message",
                    "type",
                ):
                    inner = parsed[key]
                    if isinstance(inner, dict):
                        parsed = inner
                    elif isinstance(inner, str):
                        try:
                            parsed = json.loads(inner)
                        except (json.JSONDecodeError, ValueError):
                            parsed = {"decision": inner}

            # Check decisions list from HITL
            decisions_val = parsed.get("decisions")
            answers_val = parsed.get("answers")
            if isinstance(decisions_val, list) and decisions_val:
                first = decisions_val[0]
                if isinstance(first, dict):
                    raw_dec = first.get("type") or first.get("decision") or "confirm"
                    feedback = first.get("message") or first.get("feedback")
                else:
                    raw_dec = str(first)
                    feedback = None
            elif isinstance(answers_val, list) and answers_val:
                raw_dec = answers_val[0]
                feedback = None
            else:
                raw_dec = (
                    parsed.get("decision")
                    or parsed.get("action")
                    or parsed.get("choice")
                    or parsed.get("type")
                    or parsed.get("status")
                    or "confirm"
                )
                feedback = parsed.get("feedback") or parsed.get("rejectionReason") or parsed.get("message")

            raw_str = str(raw_dec).lower().strip()
            fb_str = str(feedback).strip() if feedback else None

            # Extract criteria if present
            criteria_list: list[str] | None = None
            crit_val = parsed.get("criteria")
            if isinstance(crit_val, list):
                criteria_list = [str(x).strip() for x in crit_val if str(x).strip()]
            elif isinstance(crit_val, str) and crit_val.strip():
                criteria_list = [
                    line.strip().lstrip("-*•0123456789.) ").strip()
                    for line in crit_val.splitlines()
                    if line.strip().lstrip("-*•0123456789.) ").strip()
                ]

            if raw_str in _EDIT_DECISIONS:
                return cls(
                    decision="edit",
                    criteria=criteria_list,
                    feedback=fb_str,
                )
            if raw_str in _REJECT_DECISIONS or (fb_str and raw_str not in _CONFIRM_DECISIONS):
                return cls(
                    decision="reject",
                    criteria=criteria_list,
                    feedback=fb_str,
                )
            if raw_str in _CANCEL_DECISIONS:
                return cls(
                    decision="cancel",
                    criteria=criteria_list,
                    feedback=fb_str,
                )
            return cls(
                decision="confirm",
                criteria=criteria_list,
                feedback=fb_str,
            )

        if isinstance(parsed, str):
            s = parsed.lower().strip()
            if s in _EDIT_DECISIONS:
                return cls(decision="edit")
            if s in _REJECT_DECISIONS:
                return cls(decision="reject")
            if s in _CANCEL_DECISIONS:
                return cls(decision="cancel")
            return cls(decision="confirm")

        return cls(decision="confirm")


# ─────────────────────────────────────────────────────────────────────────────
# 5. Export Definitions
# ─────────────────────────────────────────────────────────────────────────────

__all__ = [
    # HITL & Tool Approval
    "ActionRequest",
    "ApproveDecision",
    "AutoApproveAllDecision",
    "EditDecision",
    "HITLRequest",
    "HitlDecision",
    "HitlResumePayload",
    "RejectDecision",
    "ReviewConfig",
    "SmartApproveAllDecision",
    "SwitchManualDecision",
    # Ask User Protocol
    "ASK_USER_ANSWERED_SUMMARY",
    "ASK_USER_CANCELLED_ANSWER",
    "ASK_USER_CANCELLED_SUMMARY",
    "ASK_USER_ERROR_ANSWER_PREFIX",
    "ASK_USER_FAILED_SUMMARY",
    "ASK_USER_NOTHING_SELECTED",
    "ASK_USER_NO_ANSWER",
    "AskUserAnswered",
    "AskUserCancelled",
    "AskUserRequest",
    "AskUserResumePayload",
    "AskUserWidgetResult",
    "CHOICE_QUESTION_TYPES",
    "Choice",
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
    # Goal & Criteria Review
    "GoalReviewRequest",
    "GoalReviewResumePayload",
    # Hook Invocation Interrupts
    "HOOK_INVOCATION_INTERRUPT_TYPE",
    "HookInvocationInterrupt",
    "HookInvocationRequest",
    "HookInvocationResponse",
    "build_hook_interrupt_payload",
    "build_hook_resume_value",
    "is_hook_interrupt_payload",
    "parse_hook_interrupt_payload",
    "parse_hook_resume_value",
]
