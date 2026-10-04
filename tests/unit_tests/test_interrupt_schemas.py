"""Comprehensive unit tests for OpsCloud's terminal-native interrupt schemas."""

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from opscloud.schema.interrupts import (
    # Tool approval / HITL
    ActionRequest,
    ApproveDecision,
    AutoApproveAllDecision,
    EditDecision,
    HITLRequest,
    HitlDecision,
    HitlResumePayload,
    RejectDecision,
    ReviewConfig,
    SmartApproveAllDecision,
    SwitchManualDecision,
    # Ask user protocol
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
    CHOICE_QUESTION_TYPES,
    Choice,
    Question,
    QuestionType,
    ask_user_answer_is_empty,
    decode_multi_select_answer,
    encode_multi_select_answer,
    format_ask_user_error_answer,
    format_ask_user_transcript,
    render_ask_user_transcript_for_display,
    # Goal / rubric review
    GoalReviewRequest,
    GoalReviewResumePayload,
    # Hooks interrupt
    HOOK_INVOCATION_INTERRUPT_TYPE,
    HookInvocationInterrupt,
    build_hook_interrupt_payload,
    is_hook_interrupt_payload,
)


class TestHitlSchemas:
    """Test Tool Approval (HITL) models and resume payloads."""

    def test_hitl_decision_types(self):
        # Approve
        d_app = ApproveDecision()
        assert d_app.type == "approve"
        assert d_app.model_dump() == {"type": "approve"}

        # Reject
        d_rej = RejectDecision(message="Risk too high")
        assert d_rej.type == "reject"
        assert d_rej.message == "Risk too high"

        # Edit with arguments
        d_edit = EditDecision(args={"command": "terraform apply -auto-approve=false"})
        assert d_edit.type == "edit"
        assert d_edit.args == {"command": "terraform apply -auto-approve=false"}

        # Switch manual
        d_sm = SwitchManualDecision()
        assert d_sm.type == "switch_manual"

        # Auto approve all
        d_auto = AutoApproveAllDecision()
        assert d_auto.type == "auto_approve_all"

        # Smart approve all
        d_smart = SmartApproveAllDecision()
        assert d_smart.type == "smart_approve_all"

    def test_hitl_decision_to_command_dict(self):
        d1 = HitlDecision(type="approve")
        assert d1.to_command_dict() == {"type": "approve"}

        d2 = HitlDecision(type="reject", message="Unsafe deletion")
        assert d2.to_command_dict() == {"type": "reject", "message": "Unsafe deletion"}

        d3 = HitlDecision(type="edit", args={"region": "eu-central-1"})
        assert d3.to_command_dict() == {"type": "edit", "args": {"region": "eu-central-1"}}

        d4 = HitlDecision(type="smart_approve_all")
        assert d4.to_command_dict() == {"type": "smart_approve_all"}

    def test_hitl_request_typed_dict(self):
        act: ActionRequest = {"name": "aws_cli", "args": {"cmd": "ec2 describe-instances"}, "id": "call_123"}
        rev: ReviewConfig = {"action_name": "aws_cli", "allowed_decisions": ["approve", "reject", "edit"]}
        hitl_req: HITLRequest = {"action_requests": [act], "review_configs": [rev]}
        assert len(hitl_req["action_requests"]) == 1
        assert hitl_req["action_requests"][0]["name"] == "aws_cli"

    def test_hitl_resume_payload_from_raw_approve(self):
        payload = HitlResumePayload.from_raw("approve", count=2)
        assert len(payload.decisions) == 2
        assert all(d.type == "approve" for d in payload.decisions)
        assert payload.auto_approve_requested is False

        # Short tokens
        assert HitlResumePayload.from_raw("y").decisions[0].type == "approve"
        assert HitlResumePayload.from_raw("yes").decisions[0].type == "approve"
        assert HitlResumePayload.from_raw("confirm").decisions[0].type == "approve"

    def test_hitl_resume_payload_from_raw_reject(self):
        payload = HitlResumePayload.from_raw({"decision": "reject", "message": "Forbidden in prod"}, count=1)
        assert len(payload.decisions) == 1
        assert payload.decisions[0].type == "reject"
        assert payload.decisions[0].message == "Forbidden in prod"

        # Short token
        assert HitlResumePayload.from_raw("n").decisions[0].type == "reject"
        assert HitlResumePayload.from_raw("deny").decisions[0].type == "reject"

    def test_hitl_resume_payload_from_raw_edit(self):
        raw = {
            "decision": "edit",
            "args": {"command": "kubectl get pods -n kube-system"},
        }
        payload = HitlResumePayload.from_raw(raw)
        assert len(payload.decisions) == 1
        assert payload.decisions[0].type == "edit"
        assert payload.decisions[0].args == {"command": "kubectl get pods -n kube-system"}

        # Edit decision in decisions list
        raw_list = {
            "decisions": [
                {"type": "edit", "args": {"region": "us-west-2"}},
                {"type": "approve"},
            ]
        }
        payload_list = HitlResumePayload.from_raw(raw_list)
        assert len(payload_list.decisions) == 2
        assert payload_list.decisions[0].type == "edit"
        assert payload_list.decisions[0].args == {"region": "us-west-2"}
        assert payload_list.decisions[1].type == "approve"

    def test_hitl_resume_payload_from_raw_switch_manual(self):
        payload = HitlResumePayload.from_raw("switch_manual")
        assert payload.decisions[0].type == "switch_manual"

        payload_dict = HitlResumePayload.from_raw({"decision": "manual"})
        assert payload_dict.decisions[0].type == "switch_manual"

    def test_hitl_resume_payload_from_raw_auto_approve(self):
        payload = HitlResumePayload.from_raw("auto")
        assert payload.auto_approve_requested is True
        assert payload.decisions[0].type == "approve"

        payload2 = HitlResumePayload.from_raw({"decision": "auto_approve_all"})
        assert payload2.auto_approve_requested is True

    def test_hitl_resume_payload_from_raw_smart_approve(self):
        payload = HitlResumePayload.from_raw("smart")
        assert payload.smart_approve_requested is True
        assert payload.auto_approve_requested is True
        assert payload.decisions[0].type == "approve"

        payload_s = HitlResumePayload.from_raw("s")
        assert payload_s.smart_approve_requested is True

        payload_dict = HitlResumePayload.from_raw({"decision": "smart_approve_all"})
        assert payload_dict.smart_approve_requested is True
        assert payload_dict.auto_approve_requested is True
        assert payload_dict.decisions[0].type == "approve"

    def test_hitl_resume_payload_to_command_value(self):
        payload = HitlResumePayload(
            decisions=[
                HitlDecision(type="approve"),
                HitlDecision(type="edit", args={"replicas": 3}),
            ]
        )
        cmd_val = payload.to_command_value()
        assert cmd_val == {
            "decisions": [
                {"type": "approve"},
                {"type": "edit", "args": {"replicas": 3}},
            ]
        }


class TestAskUserProtocol:
    """Test interactive ask_user question models, encodings, and serializers."""

    def test_question_types_and_choice_requirement(self):
        assert "text" not in CHOICE_QUESTION_TYPES
        assert "multiple_choice" in CHOICE_QUESTION_TYPES
        assert "multi_select" in CHOICE_QUESTION_TYPES

    def test_multi_select_encoding_roundtrip(self):
        selected = ["us-east-1", "us-west-2", "eu-west-1"]
        encoded = encode_multi_select_answer(selected)
        assert isinstance(encoded, str)
        assert encoded == '["us-east-1", "us-west-2", "eu-west-1"]'

        decoded = decode_multi_select_answer(encoded)
        assert decoded == selected

        # Empty selection
        empty_enc = encode_multi_select_answer([])
        assert empty_enc == "[]"
        assert decode_multi_select_answer(empty_enc) == []

        # Invalid JSON decoding
        assert decode_multi_select_answer("not json") is None
        assert decode_multi_select_answer("[1, 2]") is None

    def test_ask_user_answer_is_empty(self):
        # Text question
        assert ask_user_answer_is_empty("", "text") is True
        assert ask_user_answer_is_empty("   ", "text") is True
        assert ask_user_answer_is_empty("production", "text") is False

        # Multi-select question
        assert ask_user_answer_is_empty("[]", "multi_select") is True
        assert ask_user_answer_is_empty('["item"]', "multi_select") is False
        assert ask_user_answer_is_empty("malformed", "multi_select") is True

    def test_format_and_render_transcript(self):
        questions: list[Question] = [
            {"question": "Target VPC?", "type": "text"},
            {
                "question": "Select target subnets",
                "type": "multi_select",
                "choices": [{"value": "subnet-a"}, {"value": "subnet-b"}],
            },
        ]
        answers = ["vpc-12345", encode_multi_select_answer(["subnet-a", "subnet-b"])]
        transcript = format_ask_user_transcript(questions, answers)
        assert "Q: Target VPC?\nA: vpc-12345" in transcript
        assert 'Q: Select target subnets\nA: ["subnet-a", "subnet-b"]' in transcript

        display = render_ask_user_transcript_for_display(questions, transcript)
        assert display is not None
        assert "Q: Select target subnets\nA: subnet-a\nsubnet-b" in display

    def test_ask_user_resume_payload_from_raw(self):
        # Single choice answer
        p1 = AskUserResumePayload.from_raw({"choice": "us-east-1"})
        assert p1.status == "answered"
        assert p1.answers == ["us-east-1"]

        # Cancelled
        p_cancel = AskUserResumePayload.from_raw("cancelled", questions=[1, 2])
        assert p_cancel.status == "cancelled"
        assert p_cancel.answers == [ASK_USER_CANCELLED_ANSWER, ASK_USER_CANCELLED_ANSWER]

        # Error
        p_err = AskUserResumePayload.from_raw({"status": "error", "error": "timeout"}, questions=[1])
        assert p_err.status == "error"
        assert p_err.error == "timeout"
        assert p_err.answers == [format_ask_user_error_answer("timeout")]

        # Command value
        cmd = p1.to_command_value()
        assert cmd == {"status": "answered", "answers": ["us-east-1"]}


class TestGoalReviewSchemas:
    """Test Goal Review and Acceptance Criteria models."""

    def test_goal_review_resume_payload_confirm(self):
        payload = GoalReviewResumePayload.from_raw("confirm")
        assert payload.decision == "confirm"
        assert payload.criteria is None

    def test_goal_review_resume_payload_edit(self):
        raw = {
            "decision": "edit",
            "criteria": ["Check IAM policy", "Verify CloudWatch alarm"],
            "feedback": "Add alarm threshold",
        }
        payload = GoalReviewResumePayload.from_raw(raw)
        assert payload.decision == "edit"
        assert payload.criteria == ["Check IAM policy", "Verify CloudWatch alarm"]
        assert payload.feedback == "Add alarm threshold"

    def test_goal_review_resume_payload_reject(self):
        raw = {"decision": "reject", "feedback": "Need to rollback instead"}
        payload = GoalReviewResumePayload.from_raw(raw)
        assert payload.decision == "reject"
        assert payload.feedback == "Need to rollback instead"

    def test_goal_review_to_command_value(self):
        payload = GoalReviewResumePayload(decision="confirm")
        assert payload.to_command_value() == {"decision": "confirm"}


class TestHookInvocationInterrupt:
    """Test hook invocation interrupt helpers."""

    def test_hook_interrupt_check(self):
        assert is_hook_interrupt_payload({"type": "hook_invocation"}) is True
        assert is_hook_interrupt_payload({"type": "ask_user"}) is False
        assert is_hook_interrupt_payload("not_a_dict") is False
