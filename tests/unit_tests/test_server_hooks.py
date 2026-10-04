"""Unit tests for ServerHooksMiddleware, Hooks v2 models, wire tools, and interrupt transport."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from opscloud.hooks.env import (
    is_secret_env_var,
    sanitize_hook_environ,
)
from opscloud.hooks.interrupt import (
    build_hook_interrupt_payload,
    build_hook_resume_value,
    is_hook_interrupt_payload,
    parse_hook_interrupt_payload,
    parse_hook_resume_value,
)
from opscloud.hooks.models import (
    AgentIdentity,
    CommandHandlerSpec,
    CompactTrigger,
    HookContext,
    HookDecision,
    HookDomainEvent,
    HookEvent,
    HookInvocation,
    HooksConfig,
    MatcherGroup,
    NotificationDecision,
    NotificationEvent,
    OpscloudNotification,
    OpscloudNotificationKind,
    PermissionEffect,
    PermissionRequestDecision,
    PermissionRequestEvent,
    PostToolUseDecision,
    PostToolUseEvent,
    PostToolUseFailureDecision,
    PostToolUseFailureEvent,
    PreCompactDecision,
    PreCompactEvent,
    PreToolUseDecision,
    PreToolUseEvent,
    SessionEndCause,
    SessionEndDecision,
    SessionEndEvent,
    SessionStartCause,
    SessionStartDecision,
    SessionStartEvent,
    StopDecision,
    StopEvent,
    SubagentStartDecision,
    SubagentStartEvent,
    SubagentStopDecision,
    SubagentStopEvent,
    ToolCallData,
    UserPromptSubmitDecision,
    UserPromptSubmitEvent,
)
from opscloud.hooks.models.adapters import (
    HOOK_DECISION_ADAPTER,
    HOOK_DOMAIN_EVENT_ADAPTER,
    HOOK_INVOCATION_ADAPTER,
)
from opscloud.hooks.models.transport import (
    HookInvocationRequest,
    HookInvocationResponse,
)
from opscloud.hooks.tools import (
    format_mcp_wire_name,
    to_wire_call,
    to_wire_tool_input,
    to_wire_tool_name,
)
from opscloud.middleware.server_hooks import (
    ServerHooksMiddleware,
    ServerHooksState,
    _event_enabled,
    _session_gate,
    hook_decided_permission,
)
from opscloud.security.approval_mode import ApprovalMode


class TestHooksWireTools:
    """Tests for native to wire tool name and parameter projection."""

    def test_native_to_wire_tool_name(self) -> None:
        """Verify native file and shell tools map to compatible wire names."""
        assert to_wire_tool_name("execute") == "Bash"
        assert to_wire_tool_name("write_file") == "Write"
        assert to_wire_tool_name("edit_file") == "Edit"
        assert to_wire_tool_name("read_file") == "Read"
        assert to_wire_tool_name("glob") == "Glob"
        assert to_wire_tool_name("grep") == "Grep"
        assert to_wire_tool_name("ls") == "LS"
        assert to_wire_tool_name("web_search") == "WebSearch"
        assert to_wire_tool_name("fetch_url") == "WebFetch"
        assert to_wire_tool_name("fetch_web_page") == "WebFetch"
        assert to_wire_tool_name("custom_aws_tool") == "custom_aws_tool"

    def test_mcp_wire_name_mapping(self) -> None:
        """Verify MCP tools format properly into wire representation."""
        assert format_mcp_wire_name("aws", "describe_instances") == "mcp__aws__describe_instances"
        assert to_wire_tool_name("describe_instances", mcp_server="aws") == "mcp__aws__describe_instances"
        assert to_wire_tool_name("aws_describe_instances", mcp_server="aws") == "mcp__aws__describe_instances"
        assert to_wire_tool_name("mcp__aws__describe_instances") == "mcp__aws__describe_instances"

    def test_to_wire_call_execute(self) -> None:
        """Verify execute command timeout converts from seconds to milliseconds."""
        call = ToolCallData(
            id="call-1",
            name="execute",
            args={"command": "aws s3 ls", "timeout": 15},
        )
        wire_name, wire_args = to_wire_call(call)
        assert wire_name == "Bash"
        assert wire_args["command"] == "aws s3 ls"
        assert wire_args["timeout"] == 15000

    def test_to_wire_call_read_file(self) -> None:
        """Verify read_file offset maps from 0-indexed to 1-indexed."""
        call = ToolCallData(
            id="call-2",
            name="read_file",
            args={"file_path": "config.yaml", "offset": 0, "limit": 50},
        )
        wire_name, wire_args = to_wire_call(call)
        assert wire_name == "Read"
        assert wire_args["file_path"] == "config.yaml"
        assert wire_args["offset"] == 1
        assert wire_args["limit"] == 50

    def test_to_wire_call_grep(self) -> None:
        """Verify grep pattern escaping and head_limit mapping."""
        call = ToolCallData(
            id="call-3",
            name="grep",
            args={"path": "src/", "pattern": "def foo.*", "max_count": 10},
        )
        wire_name, wire_args = to_wire_call(call)
        assert wire_name == "Grep"
        assert wire_args["path"] == "src/"
        assert wire_args["head_limit"] == 10

    def test_to_wire_call_mcp_passthrough(self) -> None:
        """Verify MCP calls preserve their argument dictionary unmodified."""
        call = ToolCallData(
            id="call-4",
            name="list_buckets",
            args={"region": "us-east-1"},
            mcp_server="aws",
        )
        wire_name, wire_args = to_wire_call(call)
        assert wire_name == "mcp__aws__list_buckets"
        assert wire_args == {"region": "us-east-1"}


class TestServerHooksMiddleware:
    """Tests for ServerHooksMiddleware lifecycle, session gating, and permissions."""

    def test_instantiation_defaults(self, tmp_path: Path) -> None:
        """Verify middleware initializes with provided or default directory."""
        mw = ServerHooksMiddleware(cwd=tmp_path, emit_stop=True)
        assert mw._cwd == tmp_path
        assert mw._emit_stop is True
        assert mw.state_schema == ServerHooksState

    def test_session_gate_valid(self) -> None:
        """Verify valid session gate parses events and snapshot id correctly."""
        context = {
            "hooks_snapshot_id": "snap-xyz",
            "hooks_server_events": ["PreToolUse", "PostToolUse", "Stop"],
        }
        gate = _session_gate(context)
        assert gate is not None
        assert gate["snapshot_id"] == "snap-xyz"
        assert _event_enabled(gate, HookEvent.PRE_TOOL_USE)
        assert _event_enabled(gate, HookEvent.POST_TOOL_USE)
        assert _event_enabled(gate, HookEvent.STOP)
        assert not _event_enabled(gate, HookEvent.SESSION_START)

    def test_session_gate_empty(self) -> None:
        """Verify empty or missing context produces None gate."""
        assert _session_gate({}) is None
        assert _session_gate(None) is None

    def test_before_model_disabled_events_returns_empty(self, tmp_path: Path) -> None:
        """Verify before_model returns empty outcomes map when no gate is present."""
        mw = ServerHooksMiddleware(cwd=tmp_path)
        runtime = MagicMock()
        runtime.context = {}
        state: ServerHooksState = {"messages": []}
        update = mw.before_model(state, runtime)
        assert update == {"_hooks_pre_tool_outcomes": {}}

    def test_after_agent_emit_stop_disabled(self, tmp_path: Path) -> None:
        """Verify after_agent returns None when emit_stop is False."""
        mw = ServerHooksMiddleware(cwd=tmp_path, emit_stop=False)
        runtime = MagicMock()
        runtime.context = {
            "hooks_snapshot_id": "snap-123",
            "hooks_server_events": [HookEvent.STOP.value],
        }
        state: ServerHooksState = {"messages": [AIMessage(content="Finished task.")]}
        result = mw.after_agent(state, runtime)
        assert result is None

    def test_hook_decided_permission(self) -> None:
        """Verify hook_decided_permission identifies whether a tool call has an outcome."""
        state = {
            "_hooks_pre_tool_outcomes": {
                "call-1": {"behavior": "allow", "context": []},
                "call-2": {"behavior": "deny", "reason": "Blocked by policy", "context": []},
                "call-3": {"behavior": "none", "context": []},
            }
        }
        assert hook_decided_permission(state, "call-1") is True
        assert hook_decided_permission(state, "call-2") is True
        assert hook_decided_permission(state, "call-3") is False
        assert hook_decided_permission(state, "call-unknown") is False
        assert hook_decided_permission(None, "call-1") is False


class TestInterruptTransport:
    """Tests for HookInvocationInterrupt serialization and resume validation."""

    def test_interrupt_payload_serialization_roundtrip(self) -> None:
        """Verify HookInvocationRequest serializes to JSON interrupt payload and back."""
        inv_id = uuid4()
        context = HookContext(
            thread_id="thread-abc",
            cwd=Path("/work"),
            approval_mode=ApprovalMode.MANUAL,
        )
        event = PreToolUseEvent(
            event=HookEvent.PRE_TOOL_USE,
            call=ToolCallData(id="call-9", name="execute", args={"command": "uptime"}),
        )
        request = HookInvocationRequest(
            protocol_version=1,
            invocation_id=inv_id,
            snapshot_id="snap-456",
            run_id="run-456",
            invocation=HookInvocation(context=context, event=event),
            deadline=datetime.now() + timedelta(seconds=30),
        )

        payload = build_hook_interrupt_payload(request)
        assert payload["type"] == "hook_invocation"
        assert payload["request"]["protocol_version"] == 1

        parsed = parse_hook_interrupt_payload(payload)
        assert parsed is not None
        assert parsed.invocation_id == inv_id
        assert parsed.snapshot_id == "snap-456"
        assert parsed.invocation.event.event == HookEvent.PRE_TOOL_USE

    def test_parse_hook_interrupt_payload_invalid(self) -> None:
        """Verify non-hook interrupt values return None."""
        assert parse_hook_interrupt_payload(None) is None
        assert parse_hook_interrupt_payload({"type": "other_interrupt"}) is None
        assert parse_hook_interrupt_payload("string") is None

    def test_resume_value_parsing_success(self) -> None:
        """Verify valid resume response parses successfully."""
        inv_id = uuid4()
        snapshot_id = "snap-789"
        decision = PreToolUseDecision(
            event=HookEvent.PRE_TOOL_USE,
            permission=PermissionEffect(behavior="allow"),
        )
        response = HookInvocationResponse(
            protocol_version=1,
            invocation_id=inv_id,
            snapshot_id=snapshot_id,
            decision=decision,
        )

        resume_dict = build_hook_resume_value(response)
        parsed = parse_hook_resume_value(
            resume_dict,
            invocation_id=inv_id,
            snapshot_id=snapshot_id,
        )
        assert parsed.invocation_id == inv_id
        assert parsed.snapshot_id == snapshot_id
        assert parsed.decision.event == HookEvent.PRE_TOOL_USE

    def test_resume_value_parsing_mismatch_errors(self) -> None:
        """Verify mismatched IDs or invalid types raise ValueError."""
        inv_id = uuid4()
        snapshot_id = "snap-789"
        response = HookInvocationResponse(
            protocol_version=1,
            invocation_id=inv_id,
            snapshot_id=snapshot_id,
            decision=PreToolUseDecision(
                event=HookEvent.PRE_TOOL_USE,
                permission=PermissionEffect(behavior="allow"),
            ),
        )
        resume_dict = build_hook_resume_value(response)

        # Mismatched invocation_id
        with pytest.raises(ValueError, match="Resume invocation id"):
            parse_hook_resume_value(
                resume_dict,
                invocation_id=uuid4(),
                snapshot_id=snapshot_id,
            )

        # Mismatched snapshot_id
        with pytest.raises(ValueError, match="Resume snapshot id"):
            parse_hook_resume_value(
                resume_dict,
                invocation_id=inv_id,
                snapshot_id="wrong-snapshot",
            )

        # Non-dict value
        with pytest.raises(ValueError, match="must be a JSON object"):
            parse_hook_resume_value(
                "invalid",
                invocation_id=inv_id,
                snapshot_id=snapshot_id,
            )


class TestHookDomainModels:
    """Tests for hook domain event and decision models."""

    def test_post_tool_use_from_tool_message(self) -> None:
        """Verify PostToolUseEvent constructs correctly from ToolMessage."""
        call = ToolCallData(id="call-t1", name="ls", args={"path": "."})
        msg = ToolMessage(content="file1.txt\nfile2.txt", tool_call_id="call-t1")
        event = PostToolUseEvent.from_tool_result(msg, call=call, duration_ms=42)

        assert event.event == HookEvent.POST_TOOL_USE
        assert event.duration_ms == 42
        assert isinstance(event.result, dict)
        assert event.result.get("content") == "file1.txt\nfile2.txt"

    def test_post_tool_use_from_command(self) -> None:
        """Verify PostToolUseEvent constructs correctly from Command."""
        call = ToolCallData(id="call-t2", name="execute", args={"command": "date"})
        cmd = Command(update={"output": "Thu Sep 25"})
        event = PostToolUseEvent.from_tool_result(cmd, call=call, duration_ms=10)

        assert event.event == HookEvent.POST_TOOL_USE
        assert event.duration_ms == 10

    def test_opscloud_notification_models(self) -> None:
        """Verify OpscloudNotification and OpscloudNotificationKind function as expected."""
        assert OpscloudNotificationKind.PERMISSION_REQUIRED == "permission_required"
        assert OpscloudNotificationKind.AGENT_NEEDS_INPUT == "agent_needs_input"
        assert OpscloudNotificationKind.AGENT_COMPLETED == "agent_completed"

        notification = OpscloudNotification(
            type=OpscloudNotificationKind.AGENT_COMPLETED,
            message="Cluster provisioned.",
            title="Success",
        )
        event = NotificationEvent(
            event=HookEvent.NOTIFICATION,
            notification=notification,
        )
        assert event.notification.type == "agent_completed"
        assert event.notification.message == "Cluster provisioned."
        assert event.notification.title == "Success"

    def test_post_tool_use_failure_event_and_decision(self) -> None:
        """Verify PostToolUseFailureEvent and PostToolUseFailureDecision."""
        call = ToolCallData(
            id="call-err-1",
            name="execute",
            args={"command": "aws s3 cp non-existent s3://bucket/"},
        )
        failure_event = PostToolUseFailureEvent(
            event=HookEvent.POST_TOOL_USE_FAILURE,
            call=call,
            error="fatal: path not found",
            duration_ms=120,
        )
        assert failure_event.event == HookEvent.POST_TOOL_USE_FAILURE
        assert failure_event.error == "fatal: path not found"
        assert failure_event.duration_ms == 120

        decision = PostToolUseFailureDecision(
            event=HookEvent.POST_TOOL_USE_FAILURE,
            feedback=["Please check the source file path before retrying."],
            context=["s3-bucket: bucket"],
        )
        assert decision.event == HookEvent.POST_TOOL_USE_FAILURE
        assert len(decision.feedback) == 1
        assert len(decision.context) == 1


class TestHooksConfigModels:
    """Tests for Hooks v2 declarative configuration schemas."""

    def test_command_handler_spec_validation(self) -> None:
        """Verify CommandHandlerSpec parses command, argv, and timeout."""
        spec = CommandHandlerSpec(
            type="command",
            command="audit_aws.sh",
            argv=["audit_aws.sh", "--verbose"],
            timeout=10.0,
            status_message="Running AWS audit hook",
        )
        assert spec.type == "command"
        assert spec.command == "audit_aws.sh"
        assert spec.argv == ["audit_aws.sh", "--verbose"]
        assert spec.timeout == 10.0
        assert spec.status_message == "Running AWS audit hook"

    def test_command_handler_spec_invalid_argv(self) -> None:
        """Verify invalid argv throws ValueError."""
        with pytest.raises(ValueError, match="argv must be a non-empty list"):
            CommandHandlerSpec(type="command", command="test", argv=[])

        with pytest.raises(ValueError, match="argv\\[0\\] must be a non-empty"):
            CommandHandlerSpec(type="command", command="test", argv=["   "])

    def test_hooks_config_grouping(self) -> None:
        """Verify HooksConfig organizes MatcherGroups by HookEvent."""
        config = HooksConfig(
            hooks={
                HookEvent.PRE_TOOL_USE: [
                    MatcherGroup(
                        matcher="Bash",
                        hooks=[CommandHandlerSpec(type="command", command="check_safety.sh")],
                    )
                ],
                HookEvent.POST_TOOL_USE_FAILURE: [
                    MatcherGroup(
                        matcher="execute",
                        hooks=[CommandHandlerSpec(type="command", command="alert_failure.sh")],
                    )
                ],
            }
        )
        assert HookEvent.PRE_TOOL_USE in config.hooks
        assert config.hooks[HookEvent.PRE_TOOL_USE][0].matcher == "Bash"
        assert len(config.hooks[HookEvent.POST_TOOL_USE_FAILURE][0].hooks) == 1


class TestHookEnvironment:
    """Tests for hook subprocess environment sanitization."""

    def test_is_secret_env_var(self) -> None:
        """Verify secret markers identify credentials."""
        assert is_secret_env_var("OPENAI_API_KEY") is True
        assert is_secret_env_var("AWS_SECRET_ACCESS_KEY") is True
        assert is_secret_env_var("AWS_SESSION_TOKEN") is True
        assert is_secret_env_var("DB_PASSWORD") is True
        assert is_secret_env_var("GITHUB_TOKEN") is True
        assert is_secret_env_var("PATH") is False
        assert is_secret_env_var("HOME") is False
        assert is_secret_env_var("AWS_REGION") is False
        assert is_secret_env_var("USER") is False

    def test_sanitize_hook_environ(self) -> None:
        """Verify sanitize_hook_environ strips secrets from dictionary."""
        mock_env = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/Users/test",
            "AWS_REGION": "us-east-1",
            "OPENAI_API_KEY": "sk-secret-12345",
            "AWS_SECRET_ACCESS_KEY": "aws-secret-xyz",
            "ANTHROPIC_API_KEY": "ant-secret-999",
        }
        sanitized = sanitize_hook_environ(mock_env)
        assert "PATH" in sanitized
        assert "HOME" in sanitized
        assert "AWS_REGION" in sanitized
        assert "OPENAI_API_KEY" not in sanitized
        assert "AWS_SECRET_ACCESS_KEY" not in sanitized
        assert "ANTHROPIC_API_KEY" not in sanitized


class TestInterruptPredicate:
    """Tests for is_hook_interrupt_payload."""

    def test_is_hook_interrupt_payload(self) -> None:
        """Verify is_hook_interrupt_payload identifies hook interrupts."""
        assert is_hook_interrupt_payload({"type": "hook_invocation", "request": {}}) is True
        assert is_hook_interrupt_payload({"type": "other_interrupt"}) is False
        assert is_hook_interrupt_payload({}) is False
        assert is_hook_interrupt_payload("some string") is False
        assert is_hook_interrupt_payload(None) is False

