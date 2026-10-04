"""Unit tests for GoalCriteriaMiddleware, GoalProposal, and criteria agent factories."""

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import ValidationError

from opscloud.middleware.goal_criteria import (
    GoalAmendRequest,
    GoalCreateRequest,
    GoalCriteriaMiddleware,
    GoalProposal,
    _REPOSITORY_RECURSION_LIMIT,
    _STRUCTURED_OUTPUT_TOOL_NAME,
    _WEB_SEARCH_CALL_LIMIT,
    _raise_terminal_goal_state_size_error,
    create_goal_criteria_agent,
    create_goal_criteria_fallback_agent,
)
from opscloud.state.goal_state_limits import (
    GOAL_APPLICATION_CHAR_LIMIT,
    GOAL_OBJECTIVE_CHAR_LIMIT,
    RUBRIC_CHAR_LIMIT,
    GoalStateSizeError,
)


class TestGoalProposal:
    def test_valid_proposal(self):
        prop = GoalProposal(
            objective="Deploy AWS RDS Postgres instance",
            criteria="- Multi-AZ enabled\n- KMS encryption enabled\n- Subnet group configured",
        )
        assert prop.objective == "Deploy AWS RDS Postgres instance"
        assert "Multi-AZ" in prop.criteria

    def test_empty_objective_rejected(self):
        with pytest.raises(ValidationError):
            GoalProposal(objective="   ", criteria="- Valid criterion")

    def test_empty_criteria_rejected(self):
        with pytest.raises(ValidationError):
            GoalProposal(objective="Valid objective", criteria="   ")

    def test_overlong_individual_field_rejected(self):
        with pytest.raises(ValidationError):
            GoalProposal(
                objective="x" * (GOAL_OBJECTIVE_CHAR_LIMIT + 1),
                criteria="- Criteria",
            )
        with pytest.raises(ValidationError):
            GoalProposal(
                objective="Valid objective",
                criteria="x" * (RUBRIC_CHAR_LIMIT + 1),
            )

    def test_combined_length_exceeded_rejected(self):
        # Even if individual fields fit their limits (8k and 12k), their sum cannot exceed 12k
        with pytest.raises(ValidationError) as exc_info:
            GoalProposal(
                objective="o" * 7_000,
                criteria="c" * 7_000,
            )
        assert "Goal objective and criteria combined is 14,000 characters" in str(exc_info.value)


class TestTerminalGoalStateSizeError:
    def test_combined_overshoot_raises_terminal_size_error(self):
        # Construct a fake structured-output exception where tool call arguments exceed 12k combined
        msg = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "GoalProposal",
                    "args": {
                        "objective": "o" * 7_000,
                        "criteria": "c" * 7_000,
                    },
                    "id": "call_1",
                }
            ],
        )
        exc = Exception("Structured output validation failed")
        setattr(exc, "ai_message", msg)

        # Must raise GoalStateSizeError directly (terminal, not retried)
        with pytest.raises(GoalStateSizeError):
            _raise_terminal_goal_state_size_error(exc)

    def test_per_field_overshoot_returns_retry_feedback(self):
        # When an individual field overshot its max_length (e.g. objective > 8000),
        # this is published in the schema so the model should be allowed to retry
        msg = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "GoalProposal",
                    "args": {
                        "objective": "o" * 8_500,
                        "criteria": "c" * 500,
                    },
                    "id": "call_2",
                }
            ],
        )
        exc = Exception("Field max_length exceeded")
        setattr(exc, "ai_message", msg)

        res = _raise_terminal_goal_state_size_error(exc)
        assert "Please fix your mistakes" in res


class TestAgentCreation:
    def test_create_fallback_agent(self):
        model = GenericFakeChatModel(messages=iter([AIMessage(content="ok")]))
        agent = create_goal_criteria_fallback_agent(model=model)
        assert agent is not None

    def test_create_criteria_agent(self):
        from deepagents.backends import StateBackend

        model = GenericFakeChatModel(messages=iter([AIMessage(content="ok")]))
        backend = StateBackend()
        agent = create_goal_criteria_agent(
            model=model,
            repository_backend=backend,
            repository_root="/tmp/repo",
            context_tools=[],
        )
        assert agent is not None


class TestGoalCriteriaMiddleware:
    def test_update_valid_proposal(self):
        middleware = GoalCriteriaMiddleware(criteria_agent=None, fallback_agent=None)
        req: GoalCreateRequest = {
            "kind": "create",
            "request_id": "req-1",
            "objective": "Deploy Lambda function",
        }
        res = {
            "structured_response": GoalProposal(
                objective="Deploy Lambda function",
                criteria="- Handler is configured\n- Memory set to 512MB",
            )
        }
        update = middleware._update(req, res)
        assert update["_pending_goal_objective"] == "Deploy Lambda function"
        assert "- Handler is configured" in update["_pending_goal_rubric"]
        assert update["jump_to"] == "end"
        assert update["goal_criteria_request"] is None
