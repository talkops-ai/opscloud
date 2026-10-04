"""Unit tests for Jev-powered System One rubric evaluation and goal evolution."""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock
import pytest

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from deepagents.middleware.rubric import (
    CriterionFail,
    CriterionPass,
    GraderResponse,
    RubricState,
)
from langchain_typesafe import (
    ChoiceAnswer,
    ClassifierResponse,
    Noul,
    NoulAnswer,
    ScoreAnswer,
)

from opscloud.middleware.reliable_rubric import ReliableRubricMiddleware
from opscloud.rubrics.evidence_extractor import extract_grading_evidence
from opscloud.rubrics.jev_compiler import CompiledRubric, JevCriteriaCompiler
from opscloud.rubrics.jev_grader import (
    BLOCKER_DISPATCH,
    JevHybridRubricGrader,
    build_criteria_evaluations,
)
from opscloud.rubrics.evaluator import evaluate_rubric


# ── 1. Criteria Compiler Tests ─────────────────────────────────────────────


def test_jev_criteria_compiler_markdown_bullets():
    raw_markdown = """
    - 1. Deploy AWS VPC with 2 public subnets
    * 2. Configure RDS Postgres instance with multi-AZ
    - 3. Verify KMS encryption-at-rest is enabled
    """
    compiled = JevCriteriaCompiler.compile(
        objective="Deploy secure RDS VPC infrastructure",
        criteria=raw_markdown,
    )

    assert isinstance(compiled, CompiledRubric)
    assert len(compiled.criteria_map) == 3
    assert "criterion_1" in compiled.questions
    assert "criterion_2" in compiled.questions
    assert "criterion_3" in compiled.questions
    assert "readiness" in compiled.questions
    assert "blocker_status" in compiled.questions

    # Verify questions types
    assert compiled.questions["criterion_1"].type == "noul"
    assert compiled.questions["readiness"].type == "score"
    assert compiled.questions["blocker_status"].type == "choice"

    # Verify criteria text cleaning
    assert "Deploy AWS VPC" in compiled.criteria_map["criterion_1"]
    assert "Configure RDS Postgres" in compiled.criteria_map["criterion_2"]
    assert "Verify KMS encryption" in compiled.criteria_map["criterion_3"]


def test_jev_criteria_compiler_single_string():
    compiled = JevCriteriaCompiler.compile(
        objective="Single task",
        criteria="Ensure Terraform plan diff is clean",
    )
    assert len(compiled.criteria_map) == 1
    assert "criterion_1" in compiled.questions
    assert "Ensure Terraform plan diff is clean" in compiled.criteria_map["criterion_1"]


def test_jev_criteria_compiler_negative_avoidance_assertion():
    compiled = JevCriteriaCompiler.compile(
        objective="Create nginx.conf without verification",
        criteria="- Execution completes without running an nginx -t or configuration syntax validation command.",
    )
    assert len(compiled.criteria_map) == 1
    q = compiled.questions["criterion_1"]
    assert isinstance(q, Noul)
    assert q.type == "noul"
    assert q.criteria is not None
    assert isinstance(q.criteria.true, str)
    assert "NOT executed" in q.criteria.true
    assert isinstance(q.criteria.false, str)
    assert "WAS executed" in q.criteria.false


# ── 2. Evidence Extractor Tests ────────────────────────────────────────────


def test_extract_grading_evidence():
    messages = [
        HumanMessage(content="Deploy the Redis cluster"),
        AIMessage(
            content="Running deployment scripts now",
            tool_calls=[
                {"id": "call_1", "name": "execute", "args": {"command": "terraform apply -auto-approve"}},
                {"id": "call_2", "name": "write_file", "args": {"path": "redis.conf"}},
            ],
        ),
        ToolMessage(content="Apply complete! Resources: 3 added, 0 changed.", tool_call_id="call_1"),
        ToolMessage(content="File saved.", tool_call_id="call_2"),
    ]

    state = {
        "messages": messages,
        "_pending_goal_completion_note": "Redis cluster verified healthy on port 6379",
        "_goal_status_note": "Initial staging",
    }

    evidence = extract_grading_evidence(state)
    assert len(evidence["commands_executed"]) == 1
    assert evidence["commands_executed"][0]["command"] == "terraform apply -auto-approve"
    assert evidence["commands_executed"][0]["status"] == "success"
    assert "Apply complete" in evidence["commands_executed"][0]["output"]

    assert len(evidence["files_modified"]) == 1
    assert evidence["files_modified"][0]["path"] == "redis.conf"

    assert evidence["completion_evidence_note"] == "Redis cluster verified healthy on port 6379"


# ── 3. Declarative Evaluation & Dispatch Tests ─────────────────────────────


def test_build_criteria_evaluations_and_dispatch():
    criteria_map = {
        "criterion_1": "Redis cluster deployed",
        "criterion_2": "Encryption enabled",
    }

    # Case A: 100% Satisfied
    mock_response = ClassifierResponse(
        model="typesafe/jev-1.13",
        answers={
            "criterion_1": NoulAnswer(type="noul", noul=0.98),
            "criterion_2": NoulAnswer(type="noul", noul=0.92),
            "readiness": ScoreAnswer(
                type="score",
                score=1.8,
                legend={0: "Incomplete", 1: "Functional", 2: "Production Ready"},
                probabilities={0: 0.0, 1: 0.2, 2: 0.8},
                confidence=0.9,
            ),
            "blocker_status": ChoiceAnswer(
                type="choice",
                choice="progressing",
                probabilities={"progressing": 0.99, "external_blocker": 0.01},
                confidence=0.98,
            ),
        },
    )

    evals = build_criteria_evaluations(criteria_map, mock_response)
    assert len(evals) == 2
    assert all(c["passed"] for c in evals)

    handler = BLOCKER_DISPATCH["progressing"]
    verdict = handler(mock_response, evals)
    assert verdict.result == "satisfied"
    assert "All 2 criteria successfully verified" in verdict.explanation

    # Case B: Failure detected
    fail_response = ClassifierResponse(
        model="typesafe/jev-1.13",
        answers={
            "criterion_1": NoulAnswer(type="noul", noul=0.95),
            "criterion_2": NoulAnswer(type="noul", noul=0.30),  # Failing
            "readiness": ScoreAnswer(
                type="score",
                score=0.9,
                legend={},
                probabilities={},
                confidence=0.8,
            ),
            "blocker_status": ChoiceAnswer(
                type="choice",
                choice="progressing",
                probabilities={},
                confidence=0.9,
            ),
        },
    )
    fail_evals = build_criteria_evaluations(criteria_map, fail_response)
    assert fail_evals[0]["passed"] is True
    assert fail_evals[1]["passed"] is False

    fail_verdict = handler(fail_response, fail_evals)
    assert fail_verdict.result == "needs_revision"
    assert "1 of 2 criteria not yet satisfied" in fail_verdict.explanation

    # Case C: External Blocker Detected
    blocker_response = ClassifierResponse(
        model="typesafe/jev-1.13",
        answers={
            "criterion_1": NoulAnswer(type="noul", noul=0.20),
            "criterion_2": NoulAnswer(type="noul", noul=0.10),
            "readiness": ScoreAnswer(type="score", score=0.2, legend={}, probabilities={}, confidence=0.9),
            "blocker_status": ChoiceAnswer(
                type="choice",
                choice="external_blocker",
                probabilities={"external_blocker": 0.95},
                confidence=0.95,
            ),
        },
    )
    blocker_evals = build_criteria_evaluations(criteria_map, blocker_response)
    blocker_handler = BLOCKER_DISPATCH["external_blocker"]
    blocker_verdict = blocker_handler(blocker_response, blocker_evals)
    assert blocker_verdict.result == "needs_revision"
    assert "Environmental blocker detected" in blocker_verdict.explanation


# ── 4. JevHybridRubricGrader Tests with Mock Classifier ─────────────────────


@pytest.mark.asyncio
async def test_jev_grader_fast_pass_and_tier2_escalation(monkeypatch):
    grader = JevHybridRubricGrader(api_key="mock_key")
    assert grader.is_available() is True

    compiled = JevCriteriaCompiler.compile(
        objective="Deploy Redis",
        criteria="- Cluster running\n- Security group open",
    )

    mock_classifier = MagicMock()
    grader._classifier = mock_classifier

    # 1. Fast pass response
    pass_response = ClassifierResponse(
        model="typesafe/jev-1.13",
        answers={
            "criterion_1": NoulAnswer(type="noul", noul=0.99),
            "criterion_2": NoulAnswer(type="noul", noul=0.95),
            "readiness": ScoreAnswer(type="score", score=1.9, legend={}, probabilities={}, confidence=0.9),
            "blocker_status": ChoiceAnswer(type="choice", choice="progressing", probabilities={}, confidence=0.9),
        },
    )
    mock_classifier.ainvoke = AsyncMock(return_value=pass_response)

    tier2_called = False

    async def mock_tier2_llm(failing_items):
        nonlocal tier2_called
        tier2_called = True
        return GraderResponse(result="needs_revision", explanation="Remediation instructions", criteria=[])

    verdict = await grader.agrade(compiled, evidence={}, fallback_llm_fn=mock_tier2_llm)
    assert verdict.result == "satisfied"
    assert tier2_called is False  # Tier 2 LLM was not invoked on pass!

    # 2. Failing response triggers Tier 2 escalation
    fail_response = ClassifierResponse(
        model="typesafe/jev-1.13",
        answers={
            "criterion_1": NoulAnswer(type="noul", noul=0.99),
            "criterion_2": NoulAnswer(type="noul", noul=0.20),
            "readiness": ScoreAnswer(type="score", score=0.8, legend={}, probabilities={}, confidence=0.9),
            "blocker_status": ChoiceAnswer(type="choice", choice="progressing", probabilities={}, confidence=0.9),
        },
    )
    mock_classifier.ainvoke = AsyncMock(return_value=fail_response)

    verdict_fail = await grader.agrade(compiled, evidence={}, fallback_llm_fn=mock_tier2_llm)
    assert verdict_fail.result == "needs_revision"
    assert tier2_called is True  # Tier 2 LLM was invoked only on failure!


# ── 5. ReliableRubricMiddleware Smart Mode Gating Tests ────────────────────


@pytest.mark.asyncio
async def test_reliable_rubric_smart_mode_gating(monkeypatch):
    middleware = ReliableRubricMiddleware(
        model="anthropic:claude-3-5-haiku-20241022",
        max_iterations=2,
    )

    state: RubricState = cast(
        RubricState,
        {
            "messages": [HumanMessage(content="Deploy Redis")],
            "rubric": "- Port 6379 accessible",
            "_goal_objective": "Setup Redis",
            "_current_grading_run_id": "test-run",
        },
    )

    mock_jev = MagicMock()
    mock_jev.is_available.return_value = True
    middleware._jev_grader = mock_jev

    standard_llm_called = False

    def mock_grade_once(
        state: RubricState,
        iteration: int,
        correction: str | None = None,
        *,
        context: object | None = None,
    ) -> GraderResponse:
        nonlocal standard_llm_called
        standard_llm_called = True
        return GraderResponse(result="satisfied", explanation="Standard LLM pass", criteria=[])

    middleware._grade_once = mock_grade_once
    middleware._agrade_once = AsyncMock(side_effect=mock_grade_once)

    # ── Test A: Non-smart mode (Manual / Auto) ──
    # Should bypass Jev completely and invoke standard LLM grader
    res_non_smart = await middleware._ainvoke_grader(state, 0, context={"approval_mode": "manual"})
    assert res_non_smart.explanation == "Standard LLM pass"
    assert standard_llm_called is True
    assert mock_jev.agrade.called is False

    # ── Test B: Smart mode active with dict payload (as in TUI server graph) ──
    # Should engage Jev System One grader
    standard_llm_called = False
    mock_jev.agrade = AsyncMock(
        return_value=GraderResponse(
            result="satisfied",
            explanation="Jev System One pass",
            criteria=[CriterionPass(name="Port 6379 accessible", passed=True)],
        )
    )

    res_smart = await middleware._ainvoke_grader(
        state,
        0,
        context={"approval_mode": "smart", "thread_id": "thread-123"},
    )
    assert res_smart.explanation == "Jev System One pass"
    assert mock_jev.agrade.called is True
    assert standard_llm_called is False  # LLM grader not called!


# ── 6. ReliableRubricMiddleware Blocker Detection & Evolution Tests ────────


def test_reliable_rubric_blocker_detection_stream_and_state(monkeypatch):
    middleware = ReliableRubricMiddleware(
        model="anthropic:claude-3-5-haiku-20241022",
        max_iterations=2,
    )
    state: RubricState = cast(
        RubricState,
        {
            "messages": [HumanMessage(content="Deploy EKS")],
            "rubric": "- Deploy node group",
            "_goal_objective": "Setup EKS",
            "_current_grading_run_id": "blocker-run",
        },
    )

    mock_runtime = MagicMock()
    events = []
    mock_runtime.stream_writer = lambda e: events.append(e)

    blocker_response = GraderResponse(
        result="needs_revision",
        explanation="Environmental blocker detected: AWS vCPU quota exceeded for c6i.2xlarge.",
        criteria=[CriterionFail(name="Deploy node group", passed=False, gap="Quota limit reached")],
    )

    middleware._grade = MagicMock(return_value=blocker_response)

    res = middleware.after_agent(state, mock_runtime)

    # 1. Verify stream event emitted
    blocker_events = [e for e in events if e.get("type") == "rubric_blocker_detected"]
    assert len(blocker_events) == 1
    assert "AWS vCPU quota exceeded" in blocker_events[0]["explanation"]

    # 2. Verify state update marks goal as blocked
    assert res is not None
    assert res.get("_goal_status") == "blocked"
    assert "AWS vCPU quota exceeded" in res.get("_goal_status_note", "")


# ── 7. evaluate_rubric Direct Invocation Test ──────────────────────────────


def test_evaluate_rubric_with_jev(monkeypatch):
    mock_jev = MagicMock()
    mock_jev.is_available.return_value = True
    mock_jev.grade.return_value = GraderResponse(
        result="satisfied",
        explanation="Direct Jev Evaluation Passed",
        criteria=[CriterionPass(name="c1", passed=True)],
    )

    monkeypatch.setattr("opscloud.rubrics.jev_grader.JevHybridRubricGrader", lambda: mock_jev)

    # 1. When use_jev is True
    res = evaluate_rubric("- criterion 1", "evidence", use_jev=True)
    assert res.result == "satisfied"
    assert "Direct Jev Evaluation Passed" in res.explanation

    # 2. Default use_jev (None) uses standard agent
    mock_agent = MagicMock()
    mock_agent.invoke.return_value = {"structured_response": GraderResponse(result="satisfied", explanation="Agent Pass", criteria=[])}
    monkeypatch.setattr("opscloud.rubrics.evaluator.create_rubric_grader_agent", lambda **kw: mock_agent)

    res_std = evaluate_rubric("- criterion 1", "evidence")
    assert res_std.explanation == "Agent Pass"


# ── 8. Grader Tools Non-blocking & Working Directory Tests ──────────────────


def test_create_rubric_grader_tools_no_blocking_getcwd(monkeypatch, tmp_path: Path):
    from opscloud.rubrics.evaluator import _create_rubric_grader_tools
    from opscloud.server import SERVER_ENV_PREFIX

    # Ensure os.getcwd is never invoked
    def forbidden_getcwd():
        raise AssertionError("os.getcwd() was called unexpectedly!")

    monkeypatch.setattr(os, "getcwd", forbidden_getcwd)

    # 1. With repository_root provided
    tools = _create_rubric_grader_tools(repository_root=str(tmp_path))
    assert len(tools) > 0

    # 2. Without repository_root, but with server environment set
    monkeypatch.setenv(f"{SERVER_ENV_PREFIX}CWD", str(tmp_path))
    tools_server = _create_rubric_grader_tools()
    assert len(tools_server) > 0


# ── 9. Enhanced Evidence & Cascade Remediation Tests ────────────────────────


def test_extract_grading_evidence_cloud_tools_and_budget_truncation():
    messages = [
        HumanMessage(content="Provision AWS resources"),
        AIMessage(
            content="Invoking cloud tools",
            tool_calls=[
                {"id": "call_aws_1", "name": "aws_ec2_describe_instances", "args": {"Filters": [{"Name": "instance-state-name", "Values": ["running"]}]}},
                {"id": "call_mcp_1", "name": "mcp_kubernetes_get_pods", "args": {"namespace": "default"}},
            ],
        ),
        ToolMessage(content='{"Reservations": [{"Instances": [{"InstanceId": "i-12345"}]}]}', tool_call_id="call_aws_1"),
        ToolMessage(content='Error: Connection refused to api-server', tool_call_id="call_mcp_1"),
    ]
    state = {
        "messages": messages,
        "_pending_goal_completion_note": "A" * 15000,
    }
    evidence = extract_grading_evidence(state, max_total_chars=1000)
    assert len(evidence["cloud_and_mcp_tools"]) == 2
    assert evidence["cloud_and_mcp_tools"][0]["tool"] == "aws_ec2_describe_instances"
    assert evidence["cloud_and_mcp_tools"][0]["status"] == "success"
    assert evidence["cloud_and_mcp_tools"][1]["tool"] == "mcp_kubernetes_get_pods"
    assert evidence["cloud_and_mcp_tools"][1]["status"] == "error"
    assert len(evidence["completion_evidence_note"]) <= 1000


@pytest.mark.asyncio
async def test_jev_grader_extracts_evidence_when_state_passed():
    grader = JevHybridRubricGrader(api_key="mock_key")
    compiled = JevCriteriaCompiler.compile(
        objective="Deploy Redis",
        criteria="- Cluster running",
    )
    mock_classifier = MagicMock()
    grader._classifier = mock_classifier
    mock_classifier.ainvoke = AsyncMock(
        return_value=ClassifierResponse(
            model="typesafe/jev-1.13",
            answers={
                "criterion_1": NoulAnswer(type="noul", noul=0.99),
                "readiness": ScoreAnswer(type="score", score=2.0, legend={}, probabilities={}, confidence=0.95),
                "blocker_status": ChoiceAnswer(type="choice", choice="progressing", probabilities={}, confidence=0.95),
            },
        )
    )

    state = {
        "messages": [
            HumanMessage(content="Deploy"),
            AIMessage(
                content="",
                tool_calls=[{"id": "c1", "name": "execute", "args": {"command": "echo ready"}}],
            ),
            ToolMessage(content="ready", tool_call_id="c1"),
        ]
    }
    verdict = await grader.agrade(compiled, evidence=state)
    assert verdict.result == "satisfied"
    called_prompt = mock_classifier.ainvoke.call_args[0][0]
    assert "commands_executed" in called_prompt["state"]["evidence"]


def test_typed_blocker_dispatch_and_cascade():
    criteria_map = {"criterion_1": "Deploy cluster"}
    blocker_response = ClassifierResponse(
        model="typesafe/jev-1.13",
        answers={
            "criterion_1": NoulAnswer(type="noul", noul=0.10),
            "readiness": ScoreAnswer(type="score", score=0.1, legend={}, probabilities={}, confidence=0.9),
            "blocker_status": ChoiceAnswer(
                type="choice",
                choice="external_blocker",
                probabilities={"external_blocker": 0.95},
                confidence=0.95,
            ),
        },
    )
    evals = build_criteria_evaluations(criteria_map, blocker_response)
    handler = BLOCKER_DISPATCH["external_blocker"]
    verdict = handler(blocker_response, evals)
    assert getattr(verdict, "blocker_type", None) == "external_blocker"
    assert verdict.result == "needs_revision"


@pytest.mark.asyncio
async def test_jev_calibrated_threshold_user_scenario_passes_fast():
    """Verify that a 0.56 confidence on syntax check and 1.12 readiness passes immediately without Tier 2."""
    grader = JevHybridRubricGrader(api_key="mock_key")
    compiled = JevCriteriaCompiler.compile(
        objective="Create nginx.conf listening on port 80",
        criteria=(
            "- A file named nginx.conf is created.\n"
            "- nginx.conf contains an http block enclosing server block.\n"
            "- The server block includes listen directive for port 80.\n"
            "- nginx.conf has valid Nginx configuration syntax."
        ),
    )
    mock_classifier = MagicMock()
    grader._classifier = mock_classifier

    # Exact probabilities from user's jev-rubric-grader-output.json
    mock_classifier.ainvoke = AsyncMock(
        return_value=ClassifierResponse(
            model="typesafe/jev-1.13",
            answers={
                "criterion_1": NoulAnswer(type="noul", noul=0.98),
                "criterion_2": NoulAnswer(type="noul", noul=0.98),
                "criterion_3": NoulAnswer(type="noul", noul=0.98),
                "criterion_4": NoulAnswer(type="noul", noul=0.56),
                "readiness": ScoreAnswer(type="score", score=1.12, legend={}, probabilities={}, confidence=0.79),
                "blocker_status": ChoiceAnswer(type="choice", choice="progressing", probabilities={}, confidence=1.0),
            },
        )
    )

    tier2_called = False

    async def mock_tier2(failing):
        nonlocal tier2_called
        tier2_called = True
        return GraderResponse(result="needs_revision", explanation="", criteria=[])

    verdict = await grader.agrade(compiled, evidence={}, fallback_llm_fn=mock_tier2)
    assert verdict.result == "satisfied"
    assert tier2_called is False  # Zero LLM fallback calls!
    assert "All 4 criteria successfully verified" in verdict.explanation


@pytest.mark.asyncio
async def test_reliable_rubric_smart_mode_tier2_1shot_diagnostic(monkeypatch):
    """Verify that failing criteria in smart mode invoke a direct 1-shot diagnostic without tools."""
    mock_model = MagicMock()
    mock_model.ainvoke = AsyncMock(
        return_value=AIMessage(content="Run `nginx -t` to validate configuration syntax on disk.")
    )

    middleware = ReliableRubricMiddleware(
        model=mock_model,
        max_iterations=2,
    )

    state: RubricState = cast(
        RubricState,
        {
            "messages": [HumanMessage(content="Create nginx")],
            "rubric": "- Valid syntax",
            "_goal_objective": "Setup Nginx",
            "_current_grading_run_id": "diag-run",
        },
    )

    mock_jev = MagicMock()
    mock_jev.is_available.return_value = True

    # Simulate genuine failure: criteria 1 fails with p = 0.20
    async def mock_agrade(compiled, evidence, fallback_llm_fn=None):
        evals = [CriterionFail(name="Valid syntax", passed=False, gap="Syntax error")]
        if fallback_llm_fn:
            llm_res = await fallback_llm_fn(["Valid syntax"])
            return llm_res
        return GraderResponse(result="needs_revision", explanation="Failed", criteria=evals)

    mock_jev.agrade = mock_agrade
    middleware._jev_grader = mock_jev

    verdict = await middleware._ainvoke_grader(
        state,
        0,
        context={"approval_mode": "smart", "thread_id": "thread-456"},
    )

    assert verdict.result == "needs_revision"
    assert "Remediation Guidance" in verdict.explanation
    assert "Run `nginx -t`" in verdict.explanation
    # Verify mock_model.ainvoke was called directly with the prompt (1 single call, zero tool agent loops)
    assert mock_model.ainvoke.call_count == 1



