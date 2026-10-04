"""Unit tests for HITL auto mode and destructive command detection."""

import pytest

from opscloud.approval_mode import ApprovalMode
from opscloud.middleware.auto_mode_hitl import is_potentially_destructive


def test_destructive_aws_command_detection():
    # Destructive AWS operations
    is_dest, reason = is_potentially_destructive("execute_command", {"command": "aws ec2 terminate-instances --instance-ids i-12345"})
    assert is_dest is True
    assert "high-risk" in reason

    is_dest, reason = is_potentially_destructive("execute_command", {"command": "terraform destroy -auto-approve"})
    assert is_dest is True

    is_dest, reason = is_potentially_destructive("execute_command", {"command": "kubectl delete ns production"})
    assert is_dest is True

    # Safe read operations
    is_dest, reason = is_potentially_destructive("execute_command", {"command": "aws ec2 describe-instances"})
    assert is_dest is False
    assert reason == ""


def test_sensitive_file_modification_detection():
    is_dest, reason = is_potentially_destructive("write_file", {"path": "~/.aws/credentials"})
    assert is_dest is True
    assert "sensitive" in reason

    is_dest, reason = is_potentially_destructive("write_file", {"path": "src/app.py"})
    assert is_dest is False


@pytest.mark.asyncio
async def test_auto_mode_decision_plan_enforcement():
    from unittest.mock import MagicMock
    from langchain_core.messages import AIMessage, ToolMessage
    from opscloud.middleware.auto_mode_hitl import AutoModeHITLMiddleware

    mw = AutoModeHITLMiddleware()
    runtime = MagicMock()
    runtime.context = {"approval_mode": "auto"}
    runtime.store = None

    state = {
        "messages": [
            AIMessage(
                content="running commands",
                tool_calls=[
                    {"name": "execute", "args": {"command": "terraform destroy"}, "id": "call-1"},
                    {"name": "read_file", "args": {"file_path": "main.tf"}, "id": "call-2"},
                ],
            )
        ],
        "_auto_decision_plan": {
            "decisions": [
                {
                    "tool_call_id": "call-1",
                    "disposition": "policy_deny",
                    "category": "security",
                    "reason": "Destructive infrastructure command denied by policy",
                },
                {
                    "tool_call_id": "call-2",
                    "disposition": "classifier_allow",
                    "category": "read",
                    "reason": "Safe read operation",
                },
            ]
        },
    }

    from typing import Any, cast
    from langchain.agents.middleware.types import AgentState

    result = await mw.aafter_model(cast(AgentState[Any], state), runtime)
    assert result is not None
    messages = result.get("messages", [])
    assert len(messages) == 1
    denied_msg = messages[0]
    assert isinstance(denied_msg, ToolMessage)
    assert denied_msg.tool_call_id == "call-1"
    assert denied_msg.status == "error"
    assert "Destructive infrastructure command denied by policy" in denied_msg.content
    assert denied_msg.additional_kwargs.get("auto_denied") is True


def test_should_interrupt_tool_call_streamlined():
    from opscloud.agent.factory import _should_interrupt_tool_call
    from opscloud.approval_mode import ApprovalMode
    from unittest.mock import MagicMock

    # 1. SMART mode with auto_mode_enabled=True bypasses stock interrupt (middleware classifies)
    req_smart = MagicMock()
    req_smart.state = {"approval_mode": "smart"}
    req_smart.tool_call = {"name": "execute", "args": {"command": "ls"}, "id": "1"}
    assert _should_interrupt_tool_call(req_smart, auto_mode_enabled=True) is False

    # 2. AUTO mode with auto_mode_enabled=True bypasses stock interrupt
    req_auto = MagicMock()
    req_auto.state = {"approval_mode": "auto"}
    req_auto.tool_call = {"name": "execute", "args": {"command": "ls"}, "id": "2"}
    assert _should_interrupt_tool_call(req_auto, auto_mode_enabled=True) is False

    # 3. AUTO mode with auto_mode_enabled=False interrupts
    assert _should_interrupt_tool_call(req_auto, auto_mode_enabled=False) is True

    # 4. Internal AGENTS.md write never interrupts even in manual mode
    req_memory = MagicMock()
    req_memory.state = {"approval_mode": "manual"}
    req_memory.tool_call = {"name": "edit_file", "args": {"file_path": "memory/AGENTS.md"}, "id": "3"}
    assert _should_interrupt_tool_call(req_memory) is False

    # 5. Read-only filesystem tools never interrupt
    req_ro = MagicMock()
    req_ro.state = {"approval_mode": "manual"}
    req_ro.tool_call = {"name": "read_file", "args": {"file_path": "src/app.py"}, "id": "4"}
    assert _should_interrupt_tool_call(req_ro) is False

    # 6. Gated mutating tool in MANUAL mode interrupts
    req_manual = MagicMock()
    req_manual.state = {"approval_mode": "manual"}
    req_manual.tool_call = {"name": "execute", "args": {"command": "deploy.sh"}, "id": "5"}
    assert _should_interrupt_tool_call(req_manual) is True


@pytest.mark.asyncio
async def test_jev_classifier_missing_key(monkeypatch):
    from opscloud.security.jev_classifier import JevSecurityClassifier
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    classifier = JevSecurityClassifier(api_key="")
    verdict = await classifier.evaluate_call(
        tool_call_id="call-1",
        tool_name="execute",
        tool_args={"command": "ls"},
    )

    # Fail closed to human review
    assert verdict.requires_human_interrupt is True
    assert "not configured" in verdict.rationale.lower()


@pytest.mark.asyncio
async def test_jev_classifier_safe_call(monkeypatch):
    from opscloud.security.jev_classifier import JevSecurityClassifier
    from unittest.mock import AsyncMock, MagicMock, patch

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-123")
    classifier = JevSecurityClassifier()

    # Mock classifier instance
    mock_typesafe_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.nouls = {"is_mutating": MagicMock(noul=0.05)}
    mock_resp.scores = {"blast_radius": MagicMock(score=0.1)}
    mock_typesafe_client.ainvoke.return_value = mock_resp

    with patch.object(classifier, "_get_classifier", return_value=mock_typesafe_client):
        verdict = await classifier.evaluate_call(
            tool_call_id="call-2",
            tool_name="execute",
            tool_args={"command": "kubectl get pods"},
            active_environment="staging",
        )
        assert verdict.requires_human_interrupt is False
        assert verdict.is_mutating is False
        assert verdict.blast_radius == 0.1


@pytest.mark.asyncio
async def test_jev_classifier_mutating_high_blast_radius(monkeypatch):
    from opscloud.security.jev_classifier import JevSecurityClassifier
    from unittest.mock import AsyncMock, MagicMock, patch

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-123")
    classifier = JevSecurityClassifier()

    mock_typesafe_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.nouls = {"is_mutating": MagicMock(noul=0.95)}
    mock_resp.scores = {"blast_radius": MagicMock(score=1.8)}
    mock_typesafe_client.ainvoke.return_value = mock_resp

    with patch.object(classifier, "_get_classifier", return_value=mock_typesafe_client):
        verdict = await classifier.evaluate_call(
            tool_call_id="call-3",
            tool_name="execute",
            tool_args={"command": "kubectl delete deployment nginx"},
            active_environment="staging",
        )
        assert verdict.requires_human_interrupt is True
        assert verdict.is_mutating is True
        assert verdict.blast_radius == 1.8


@pytest.mark.asyncio
async def test_jev_classifier_timeout(monkeypatch):
    from opscloud.security.jev_classifier import JevSecurityClassifier
    from unittest.mock import AsyncMock, patch

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-123")
    classifier = JevSecurityClassifier(timeout_seconds=0.01)

    mock_typesafe_client = AsyncMock()
    mock_typesafe_client.ainvoke.side_effect = TimeoutError()

    with patch.object(classifier, "_get_classifier", return_value=mock_typesafe_client):
        verdict = await classifier.evaluate_call(
            tool_call_id="call-4",
            tool_name="execute",
            tool_args={"command": "some-command"},
        )
        # Must fail safe closed on timeout
        assert verdict.requires_human_interrupt is True
        assert "timed out" in verdict.rationale.lower()


@pytest.mark.asyncio
async def test_jev_classifier_shell_ast_destructive(monkeypatch):
    from opscloud.security.jev_classifier import JevSecurityClassifier
    from unittest.mock import AsyncMock, MagicMock, patch

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-123")
    classifier = JevSecurityClassifier()

    mock_typesafe_client = AsyncMock()
    mock_resp = MagicMock()
    # Even if Jev score is moderate, AST destructive flag forces interrupt
    mock_resp.nouls = {"is_mutating": MagicMock(noul=0.85)}
    mock_resp.scores = {"blast_radius": MagicMock(score=0.9)}
    mock_typesafe_client.ainvoke.return_value = mock_resp

    with patch.object(classifier, "_get_classifier", return_value=mock_typesafe_client):
        verdict = await classifier.evaluate_call(
            tool_call_id="call-ast-1",
            tool_name="execute",
            tool_args={"command": "kubectl delete pod nginx-prod"},
            active_environment="staging",
        )
        assert verdict.action_type == "shell_command"
        assert verdict.requires_human_interrupt is True
        assert "elevated blast radius" in verdict.rationale.lower() or "mutating" in verdict.rationale.lower()


@pytest.mark.asyncio
async def test_jev_classifier_mcp_destructive_hint(monkeypatch):
    from opscloud.security.jev_classifier import JevSecurityClassifier
    from langchain_core.tools import BaseTool
    from unittest.mock import AsyncMock, MagicMock, patch

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-123")
    classifier = JevSecurityClassifier()

    mock_tool = MagicMock(spec=BaseTool)
    mock_tool.description = "Terminate EC2 instances"
    mock_tool.metadata = {"_mcp_server": "aws", "destructiveHint": True}

    mock_typesafe_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.nouls = {"is_mutating": MagicMock(noul=0.90)}
    mock_resp.scores = {"blast_radius": MagicMock(score=0.8)}
    mock_typesafe_client.ainvoke.return_value = mock_resp

    with patch.object(classifier, "_get_classifier", return_value=mock_typesafe_client):
        verdict = await classifier.evaluate_call(
            tool_call_id="call-mcp-1",
            tool_name="mcp__aws__terminate_instances",
            tool_args={"instance_id": "i-0123456789"},
            active_environment="staging",
            tool=mock_tool,
        )
        assert verdict.action_type == "mcp_tool"
        assert verdict.mcp_server == "aws"
        assert verdict.requires_human_interrupt is True


@pytest.mark.asyncio
async def test_jev_classifier_subagent_dispatch(monkeypatch):
    from opscloud.security.jev_classifier import JevSecurityClassifier
    from unittest.mock import AsyncMock, MagicMock, patch

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-123")
    classifier = JevSecurityClassifier()

    mock_typesafe_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.nouls = {"is_mutating": MagicMock(noul=0.92)}
    mock_resp.scores = {"blast_radius": MagicMock(score=1.5)}
    mock_typesafe_client.ainvoke.return_value = mock_resp

    with patch.object(classifier, "_get_classifier", return_value=mock_typesafe_client):
        verdict = await classifier.evaluate_call(
            tool_call_id="call-sub-1",
            tool_name="task",
            tool_args={
                "subagent_type": "k8s-debugger",
                "description": "Delete all crashing pods in namespace kube-system",
            },
            active_environment="staging",
        )
        assert verdict.action_type == "subagent_dispatch"
        assert verdict.subagent_name == "k8s-debugger"
        assert verdict.requires_human_interrupt is True
        assert "subagent delegation" in verdict.rationale.lower() or "elevated blast radius" in verdict.rationale.lower()


@pytest.mark.asyncio
async def test_jev_classifier_intent_divergence(monkeypatch):
    from opscloud.security.jev_classifier import JevSecurityClassifier
    from unittest.mock import AsyncMock, MagicMock, patch

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-123")
    classifier = JevSecurityClassifier()

    mock_typesafe_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.nouls = {
        "is_mutating": MagicMock(noul=0.80),
        "intent_alignment": MagicMock(noul=0.15),  # Low alignment
    }
    mock_resp.scores = {"blast_radius": MagicMock(score=0.8)}
    mock_typesafe_client.ainvoke.return_value = mock_resp

    with patch.object(classifier, "_get_classifier", return_value=mock_typesafe_client):
        verdict = await classifier.evaluate_call(
            tool_call_id="call-div-1",
            tool_name="execute",
            tool_args={"command": "npm install -g malicious-pkg"},
            active_environment="staging",
            user_prompt="Can you check the current pod memory usage?",
        )
        assert verdict.requires_human_interrupt is True
        assert "diverges" in verdict.rationale.lower()
        assert verdict.intent_alignment_probability == 0.15


@pytest.mark.asyncio
async def test_jev_classifier_deep_agent_autonomous_mutation(monkeypatch):
    from opscloud.security.jev_classifier import JevSecurityClassifier
    from unittest.mock import AsyncMock, MagicMock, patch

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-123")
    classifier = JevSecurityClassifier()

    mock_typesafe_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.nouls = {"is_mutating": MagicMock(noul=0.88)}
    mock_resp.scores = {"blast_radius": MagicMock(score=1.1)}
    mock_typesafe_client.ainvoke.return_value = mock_resp

    with patch.object(classifier, "_get_classifier", return_value=mock_typesafe_client):
        verdict = await classifier.evaluate_call(
            tool_call_id="call-deep-1",
            tool_name="write_file",
            tool_args={"path": "deployment.yaml", "content": "modified: true"},
            active_environment="staging",
            caller_agent="k8s-diagnostics-subagent",
            is_deep_agent=True,
        )
        assert verdict.requires_human_interrupt is True
        assert "autonomous deep agent" in verdict.rationale.lower() or "elevated blast radius" in verdict.rationale.lower()
        assert verdict.caller_agent == "k8s-diagnostics-subagent"


def test_jev_credential_manifest_and_aliases():
    """Verify typesafe credentials registration and provider aliases."""
    from opscloud.config.manifest import get_option
    from opscloud.model.config import PROVIDER_KEY_ALIASES

    opt = get_option("credentials.typesafe")
    assert opt is not None
    assert opt.settings_field == "typesafe_api_key"
    assert opt.env_var == "TYPESAFE_API_KEY"

    assert "typesafe" in PROVIDER_KEY_ALIASES
    assert "TYPESAFE_API_KEY" in PROVIDER_KEY_ALIASES["typesafe"]
    assert "JEV_API_KEY" in PROVIDER_KEY_ALIASES["typesafe"]


def test_jev_classifier_settings_without_environ_mutation(monkeypatch):
    """Verify JevSecurityClassifier resolves settings without mutating os.environ."""
    import os
    import sys
    import opscloud.config.settings
    from opscloud.config.settings import Settings
    from opscloud.security.jev_classifier import JevSecurityClassifier

    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("JEV_API_KEY", raising=False)

    settings_mod = sys.modules["opscloud.config.settings"]
    test_settings = Settings(typesafe_api_key="settings-key-xyz")
    with monkeypatch.context() as m:
        m.setattr(settings_mod, "get_settings", lambda: test_settings)
        classifier = JevSecurityClassifier()
        assert classifier.is_available() is True
        assert classifier._api_key == "settings-key-xyz"
        assert "TYPESAFE_API_KEY" not in os.environ


def test_runtime_context_dataclass_and_execution_info():
    """Verify _runtime_context extracts fields from CLIContextSchema and execution_info."""
    from unittest.mock import MagicMock
    from opscloud.agent.config import CLIContextSchema
    from opscloud.middleware.auto_mode_hitl import (
        _execution_thread_id,
        _runtime_context,
        _thread_key,
    )
    from opscloud.security.approval_mode import approval_mode_key

    schema = CLIContextSchema(
        approval_mode="smart",
        thread_id="test-thread-999",
        smart=True,
    )

    runtime = MagicMock()
    runtime.config = {}
    runtime.context = schema
    runtime.execution_info = MagicMock(thread_id="test-thread-999", run_id="run-001")

    ctx = _runtime_context(runtime)
    assert ctx["approval_mode"] == "smart"
    assert ctx["thread_id"] == "test-thread-999"
    assert ctx["smart"] is True
    assert ctx["approval_mode_key"] == approval_mode_key("test-thread-999")
    assert ctx["run_id"] == "run-001"

    assert _execution_thread_id(runtime) == "test-thread-999"
    assert _thread_key(runtime) == approval_mode_key("test-thread-999")


def test_approval_policy_resolver_store_key_and_fallback():
    """Verify ApprovalPolicyResolver key validation rules and context mode fallback."""
    from opscloud.security.approval_mode import ApprovalMode, approval_mode_key
    from opscloud.security.approval_mode_source import ApprovalPolicyResolver

    # 1. Unknown thread_id trusts the key
    assert ApprovalPolicyResolver.validate_store_key("some_key", None) == "some_key"
    assert ApprovalPolicyResolver.validate_store_key("some_key", "") == "some_key"

    # 2. Known thread_id validates canonical match
    canon = approval_mode_key("th-1")
    assert ApprovalPolicyResolver.validate_store_key(canon, "th-1") == canon
    assert ApprovalPolicyResolver.validate_store_key("wrong_key", "th-1") is None

    # 3. Synchronous resolve with empty store falls back to context mode
    ctx = {"approval_mode": "smart", "thread_id": "th-1"}
    resolved = ApprovalPolicyResolver.resolve_sync(ctx, store=None)
    assert resolved == ApprovalMode.SMART

    ctx_auto = {"auto_approve": True}
    assert ApprovalPolicyResolver.resolve_sync(ctx_auto, store=None) == ApprovalMode.AUTO


@pytest.mark.asyncio
async def test_awrite_approval_mode_store_broadening():
    """Verify awrite_approval_mode persists to various store interfaces and in-memory."""
    from unittest.mock import AsyncMock, MagicMock
    from opscloud.security.approval_mode import (
        APPROVAL_MODE_NAMESPACE,
        ApprovalMode,
        approval_mode_key,
        awrite_approval_mode,
        get_approval_mode,
    )

    # 1. Direct aput_store_item (RemoteAgent)
    remote_agent = MagicMock()
    remote_agent.aput_store_item = AsyncMock()
    key1 = await awrite_approval_mode(remote_agent, "thread-rem-1", mode="smart")
    assert key1 == approval_mode_key("thread-rem-1")
    remote_agent.aput_store_item.assert_called_once_with(
        APPROVAL_MODE_NAMESPACE,
        approval_mode_key("thread-rem-1"),
        {"mode": "smart"},
    )
    assert get_approval_mode("thread-rem-1") == ApprovalMode.SMART

    # 2. Agent with store.put_item
    local_agent = MagicMock(spec=["store"])
    local_agent.store = MagicMock()
    local_agent.store.put_item = AsyncMock()
    key2 = await awrite_approval_mode(local_agent, "thread-loc-2", mode="auto")
    assert key2 == approval_mode_key("thread-loc-2")
    assert get_approval_mode("thread-loc-2") == ApprovalMode.AUTO


@pytest.mark.asyncio
async def test_middleware_smart_mode_with_jev():
    """Verify end-to-end AutoModeHITLMiddleware in SMART mode with mock Jev classifier."""
    from typing import Any, cast
    from unittest.mock import AsyncMock, MagicMock, patch
    from langchain.agents.middleware.types import AgentState
    from langchain_core.messages import AIMessage
    from opscloud.agent.config import CLIContextSchema
    from opscloud.middleware.auto_mode_hitl import AutoModeHITLMiddleware
    from opscloud.security.approval_mode import approval_mode_key
    from opscloud.security.jev_classifier import JevToolVerdict

    mw = AutoModeHITLMiddleware(interrupt_on={"execute": True, "write_file": True})

    # Runtime with CLIContextSchema dataclass in smart mode
    runtime = MagicMock()
    runtime.context = CLIContextSchema(
        approval_mode="smart",
        thread_id="th-smart-101",
        smart=True,
    )
    runtime.store = None
    runtime.execution_info = MagicMock(thread_id="th-smart-101", run_id="run-101")

    # Mock ModelRequest
    request = MagicMock()
    request.runtime = runtime
    request.messages = []
    request.state = {}
    tool_call = {"name": "execute", "args": {"command": "aws s3 ls"}, "id": "tc-101"}
    ai_msg = AIMessage(content="listing buckets", tool_calls=[tool_call])
    response = MagicMock(result=[ai_msg])

    async def mock_handler(req):
        return response

    # Mock Jev verdict (allow safe read command)
    mock_verdict = JevToolVerdict(
        tool_call_id="tc-101",
        tool_name="execute",
        is_mutating=False,
        mutating_probability=0.05,
        blast_radius=0.1,
        risk_level=0,
        requires_human_interrupt=False,
        rationale="Safe read-only command",
    )

    with patch.object(mw._jev_classifier, "evaluate_call", new_callable=AsyncMock) as mock_eval:
        mock_eval.return_value = mock_verdict
        ext_response = await mw.awrap_model_call(
            request,
            mock_handler,
        )

        from langchain.agents.middleware.types import ExtendedModelResponse
        assert isinstance(ext_response, ExtendedModelResponse)
        assert ext_response.command is not None and isinstance(ext_response.command.update, dict)
        plan = ext_response.command.update.get("_auto_decision_plan")
        assert plan is not None
        assert plan["mode_at_proposal"] == "smart"
        assert plan["thread_key"] == approval_mode_key("th-smart-101")
        assert len(plan["decisions"]) == 1
        assert plan["decisions"][0]["tool_call_id"] == "tc-101"
        assert plan["decisions"][0]["disposition"] == "classifier_allow"


@pytest.mark.asyncio
async def test_auto_mode_smart_mode_jev_mutating_requires_human_interrupt():
    """Verify that when Jev flags a mutating command requiring human interrupt,
    AutoModeHITLMiddleware marks disposition as 'require_human' rather than a hard denial.
    """
    from unittest.mock import AsyncMock, MagicMock, patch
    from langchain_core.messages import AIMessage
    from opscloud.agent.config import CLIContextSchema
    from opscloud.middleware.auto_mode_hitl import AutoModeHITLMiddleware
    from opscloud.security.approval_mode import approval_mode_key
    from opscloud.security.jev_classifier import JevToolVerdict

    mw = AutoModeHITLMiddleware(interrupt_on={"execute": True, "write_file": True})

    runtime = MagicMock()
    runtime.context = CLIContextSchema(
        approval_mode="smart",
        thread_id="th-smart-102",
        smart=True,
    )
    runtime.store = None
    runtime.execution_info = MagicMock(thread_id="th-smart-102", run_id="run-102")

    request = MagicMock()
    request.runtime = runtime
    request.messages = []
    request.state = {}
    tool_call = {
        "name": "execute",
        "args": {"command": "aws s3api create-bucket --bucket krayak-demo --region ap-south-1"},
        "id": "tc-102",
    }
    ai_msg = AIMessage(content="creating bucket", tool_calls=[tool_call])
    response = MagicMock(result=[ai_msg])

    async def mock_handler(req):
        return response

    mock_verdict = JevToolVerdict(
        tool_call_id="tc-102",
        tool_name="execute",
        is_mutating=True,
        mutating_probability=0.98,
        blast_radius=1.89,
        risk_level=2,
        requires_human_interrupt=True,
        rationale="Creation of S3 bucket in cloud environment",
    )

    with patch.object(mw._jev_classifier, "evaluate_call", new_callable=AsyncMock) as mock_eval:
        mock_eval.return_value = mock_verdict
        ext_response = await mw.awrap_model_call(
            request,
            mock_handler,
        )

        from langchain.agents.middleware.types import ExtendedModelResponse
        assert isinstance(ext_response, ExtendedModelResponse)
        assert ext_response.command is not None and isinstance(ext_response.command.update, dict)
        plan = ext_response.command.update.get("_auto_decision_plan")
        assert plan is not None
        assert plan["mode_at_proposal"] == "smart"
        assert plan["thread_key"] == approval_mode_key("th-smart-102")
        assert len(plan["decisions"]) == 1
        decision = plan["decisions"][0]
        assert decision["tool_call_id"] == "tc-102"
        assert decision["disposition"] == "require_human"
        assert decision["category"] == "destructive_action"
        assert "Creation of S3 bucket" in decision["reason"]


@pytest.mark.asyncio
async def test_auto_mode_aafter_model_require_human_delegates_to_manual():
    """Verify that when a decision plan has 'require_human', aafter_model invokes
    super().after_model with ApprovalMode.MANUAL to trigger LangGraph interrupt.
    """
    from typing import Any, cast
    from unittest.mock import AsyncMock, MagicMock, patch
    from langchain.agents.middleware.types import AgentState
    from langchain_core.messages import AIMessage
    from opscloud.approval_mode import ApprovalMode
    from opscloud.middleware.auto_mode_hitl import AutoModeHITLMiddleware

    mw = AutoModeHITLMiddleware()
    runtime = MagicMock()
    runtime.context = {"approval_mode": "smart"}
    runtime.store = None

    state = {
        "messages": [
            AIMessage(
                content="creating bucket",
                tool_calls=[
                    {"name": "execute", "args": {"command": "aws s3api create-bucket"}, "id": "call-create"},
                ],
            )
        ],
        "_auto_decision_plan": {
            "decisions": [
                {
                    "tool_call_id": "call-create",
                    "disposition": "require_human",
                    "category": "destructive_action",
                    "reason": "Mutating action requires human approval",
                },
            ]
        },
    }

    with patch("langchain.agents.middleware.human_in_the_loop.HumanInTheLoopMiddleware.after_model") as mock_super_after:
        mock_super_after.return_value = {"messages": []}
        result = await mw.aafter_model(cast(AgentState[Any], state), runtime)
        assert result == {"messages": [], "_auto_decision_plan": None}
        mock_super_after.assert_called_once()
        call_args = mock_super_after.call_args[0]
        assert call_args[0]["_opscloud_async_approval_routing"].mode == ApprovalMode.MANUAL


@pytest.mark.asyncio
async def test_jev_classifier_tail_risk_critical_probability(monkeypatch):
    """Verify that when expected score is below 1.2 but Critical tail probability >= 35%,
    Jev triggers human interrupt.
    """
    from opscloud.security.jev_classifier import JevSecurityClassifier
    from unittest.mock import AsyncMock, MagicMock, patch

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-123")
    classifier = JevSecurityClassifier()

    mock_typesafe_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.nouls = {"is_mutating": MagicMock(noul=0.85)}
    score_mock = MagicMock(score=1.05, confidence=0.88, probabilities={"0": 0.45, "1": 0.10, "2": 0.45})
    mock_resp.scores = {"blast_radius": score_mock}
    mock_resp.request_id = "req_tail_risk_test"
    mock_typesafe_client.ainvoke.return_value = mock_resp

    with patch.object(classifier, "_get_classifier", return_value=mock_typesafe_client):
        verdict = await classifier.evaluate_call(
            tool_call_id="call-tail-1",
            tool_name="execute",
            tool_args={"command": "kubectl delete pod my-pod -n prod"},
            active_environment="staging",
        )
        assert verdict.requires_human_interrupt is True
        assert "Critical blast radius probability (45%)" in verdict.rationale
        assert verdict.critical_probability == 0.45
        assert verdict.confidence == 0.88
        assert verdict.request_id == "req_tail_risk_test"


@pytest.mark.asyncio
async def test_jev_classifier_low_confidence_gate(monkeypatch):
    """Verify that low classifier confidence (< 0.65) escalates to human confirmation."""
    from opscloud.security.jev_classifier import JevSecurityClassifier
    from unittest.mock import AsyncMock, MagicMock, patch

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-123")
    classifier = JevSecurityClassifier()

    mock_typesafe_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.nouls = {"is_mutating": MagicMock(noul=0.75)}
    score_mock = MagicMock(score=0.90, confidence=0.52, probabilities={"0": 0.60, "1": 0.30, "2": 0.10})
    mock_resp.scores = {"blast_radius": score_mock}
    mock_typesafe_client.ainvoke.return_value = mock_resp

    with patch.object(classifier, "_get_classifier", return_value=mock_typesafe_client):
        verdict = await classifier.evaluate_call(
            tool_call_id="call-low-conf",
            tool_name="execute",
            tool_args={"command": "terraform apply -target=module.vpc"},
            active_environment="staging",
        )
        assert verdict.requires_human_interrupt is True
        assert "Low classifier confidence (0.52)" in verdict.rationale


def test_jev_classifier_tool_description_compaction():
    """Verify tool description compaction extracts the primary sentence and removes massive manuals."""
    from opscloud.security.jev_classifier import JevSecurityClassifier

    classifier = JevSecurityClassifier()
    long_desc = (
        "Executes a shell command in an isolated sandbox and returns combined stdout/stderr with the exit code.\n\n"
        "Usage:\n"
        "- Quote paths containing spaces (e.g. cd \"/path/with spaces\").\n"
        "- Chain commands with ';' or '&&'.\n"
        "- Avoid cd so working directory stays constant."
    )
    compact = classifier._compact_tool_description(long_desc)
    assert compact == "Executes a shell command in an isolated sandbox and returns combined stdout/stderr with the exit code."
    assert "Usage:" not in compact


def test_is_auto_mode_classifier_chunk():
    """Verify _is_auto_mode_classifier_chunk correctly detects internal classifier chunks identical to dcode."""
    from opscloud.ui.textual_adapter import _is_auto_mode_classifier_chunk

    assert _is_auto_mode_classifier_chunk({"lc_source": "auto_mode_classifier"}) is True
    assert _is_auto_mode_classifier_chunk({"lc_source": "other"}) is False
    assert _is_auto_mode_classifier_chunk({}) is False
    assert _is_auto_mode_classifier_chunk(None) is False


def test_auto_classifier_policy_prompt():
    """Verify auto classifier policy prompt is adapted for OpsCloud multi-cloud + coding agent."""
    from opscloud.middleware.auto_mode_hitl import _CLASSIFIER_POLICY

    assert "You are OpsCloud's action authorization classifier." in _CLASSIFIER_POLICY
    assert "K8s Autopilot" not in _CLASSIFIER_POLICY
    # Mentions cloud and infrastructure operations
    assert "cloud & infrastructure" in _CLASSIFIER_POLICY.lower()
    # Mentions coding and workspace operations
    assert "workspace & repository" in _CLASSIFIER_POLICY.lower()
    # Strict denials
    assert "destruction" in _CLASSIFIER_POLICY.lower()
    assert "production" in _CLASSIFIER_POLICY.lower()


@pytest.mark.asyncio
async def test_textual_adapter_filters_classifier_stream_chunks():
    """Verify textual adapter discards classifier chunks while preserving token accounting."""
    from unittest.mock import MagicMock
    from langchain_core.messages import AIMessageChunk
    from opscloud.ui.textual_adapter import TextualAdapter, _TurnStreamState

    mock_client = MagicMock()
    mock_messages = MagicMock()
    adapter = TextualAdapter(client=mock_client, assistant_id="opscloud", messages_widget=mock_messages)

    stream_state = _TurnStreamState()

    classifier_chunk = AIMessageChunk(
        content='{ "decisions": [ { "tool_call_id": "call_139870", "decision": "allow", "category": "other_policy", "reason": "" } ] }',
        usage_metadata={"input_tokens": 120, "output_tokens": 30, "total_tokens": 150},
    )
    meta = {
        "lc_source": "auto_mode_classifier",
        "tags": ["opscloud:auto"],
        "run_name": "opscloud_auto_classifier",
    }

    # Handle classifier stream event
    await adapter._handle_messages_stream_event(
        data=(classifier_chunk, meta),
        ns_key=(),
        is_main_agent=True,
        stream_state=stream_state,
    )

    # 1. Tokens were recorded
    assert adapter._stats.input_tokens == 120
    assert adapter._stats.output_tokens == 30
    assert stream_state.turn_context_tokens == 150

    # 2. But text was NOT appended to messages widget or pending text!
    mock_messages.append_assistant_token.assert_not_called()
    assert stream_state.pending_text_by_namespace.get((), "") == ""


def test_auto_classifier_timeout_resolution(monkeypatch):
    from opscloud.middleware.auto_mode_hitl import _resolve_classifier_timeout

    # Default should be 60.0s
    monkeypatch.delenv("OPSCLOUD_AUTO_CLASSIFIER_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("OPSCLOUD_AUTO_CLASSIFIER_TIMEOUT", raising=False)
    assert _resolve_classifier_timeout() == 60.0

    # Custom env override
    monkeypatch.setenv("OPSCLOUD_AUTO_CLASSIFIER_TIMEOUT_SECONDS", "45.5")
    assert _resolve_classifier_timeout() == 45.5


@pytest.mark.asyncio
async def test_auto_classifier_structured_output():
    from opscloud.middleware.auto_mode_hitl import (
        AutoDecision,
        AutoDecisionBatch,
        AutoDecisionCategory,
        AutoModeHITLMiddleware,
    )
    from langchain_core.messages import ToolCall
    from typing import cast
    from unittest.mock import AsyncMock, MagicMock

    mw = AutoModeHITLMiddleware(classifier_timeout_seconds=5.0)

    mock_structured = MagicMock()
    mock_structured.ainvoke = AsyncMock(
        return_value=AutoDecisionBatch(
            decisions=[
                AutoDecision(
                    tool_call_id="call_fast_1",
                    decision="allow",
                    category=AutoDecisionCategory.OTHER_POLICY,
                    reason="",
                )
            ]
        )
    )

    primary_model = MagicMock()
    primary_model.with_structured_output.return_value = mock_structured

    req = MagicMock()
    req.model = primary_model
    req.model_settings = {}
    req.runtime = MagicMock()
    req.runtime.context = {}
    req.state = {}

    calls = [
        cast(ToolCall, {"name": "aws_s3_list", "args": {}, "id": "call_fast_1", "type": "tool_call"})
    ]
    res = await mw._classify_with_llm(req, calls, {}, {})

    # Ensure model-agnostic with_structured_output was called
    primary_model.with_structured_output.assert_called_once_with(AutoDecisionBatch)
    assert len(res.decisions) == 1
    assert res.decisions[0].decision == "allow"







