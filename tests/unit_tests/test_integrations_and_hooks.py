"""Unit tests for OpsCloud integrations: event bus, hooks, notifications, identity, and stream bridge."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, patch
import uuid

import pytest

from opscloud.integrations.base import IncomingMessage, InteractionPayload
from opscloud.integrations.content_filter import filter_internal_content, is_internal_message
from opscloud.integrations.event_bus import (
    BypassTier,
    EventBus,
    ExternalEvent,
    default_unix_socket_path,
)
from opscloud.integrations.hooks import (
    HOOK_TOOL_OUTPUT_LIMIT,
    HookConfig,
    _sanitise_payload,
    dispatch_hook,
    dispatch_hook_fire_and_forget,
    drain_pending_hooks,
    load_hooks,
)
from opscloud.integrations.identity import IdentityMapper, UserIdentity
from opscloud.integrations.notifications import (
    notify_cloud_event,
    notify_deploy_event,
    notify_session_end,
    notify_session_start,
    notify_task_complete,
    notify_tool_result,
    notify_tool_use,
)
from opscloud.integrations.stream_bridge import (
    GraphStreamBridge,
    StreamSink,
    _humanize_tool_name,
)
from opscloud.integrations.thread_store import ThreadStore


# ── ThreadStore Tests ────────────────────────────────────────────────────────


def test_thread_store_resolution_and_reverse() -> None:
    store = ThreadStore()
    assert store.has_thread("terminal", "sess-1") is False

    lg_id = store.resolve("terminal", "sess-1")
    assert lg_id is not None
    assert store.has_thread("terminal", "sess-1") is True

    # Same lookup produces exact same UUID
    assert store.resolve("terminal", "sess-1") == lg_id

    # Reverse lookup
    info = store.reverse_lookup(lg_id)
    assert info is not None
    assert info["platform"] == "terminal"
    assert info["thread_id"] == "sess-1"

    # Unknown lookup returns None
    assert store.reverse_lookup("non-existent-uuid") is None


# ── Content Filter Tests ─────────────────────────────────────────────────────


def test_content_filter() -> None:
    # Internal protocol detection
    assert is_internal_message("## ACTIVE PLAN (LOCKED — DO NOT DEVIATE)") is True
    assert is_internal_message("### Execution Checklist\n⏳ (pending)") is True
    assert is_internal_message("[PLAN-APPROVED] Executing task") is True
    assert is_internal_message("[DUPLICATE CALL BLOCKED]") is True

    # User-facing message detection
    assert is_internal_message("Here are your active AWS EC2 instances:") is False
    assert is_internal_message("Cluster deployment succeeded.") is False
    assert is_internal_message("") is False

    # Filter internal lines
    raw_text = (
        "## ACTIVE PLAN (LOCKED)\n"
        "DO NOT DEVIATE\n"
        "Here are your active AWS EC2 instances:\n"
        "⏳ step 1 (pending)\n"
        "Instance i-12345 is running."
    )
    filtered = filter_internal_content(raw_text)
    assert "ACTIVE PLAN" not in filtered
    assert "DO NOT DEVIATE" not in filtered
    assert "Here are your active AWS EC2 instances:" in filtered
    assert "Instance i-12345 is running." in filtered

    assert filter_internal_content("") == ""


# ── Hooks Tests ──────────────────────────────────────────────────────────────


def test_hooks_config_and_matching() -> None:
    hook = HookConfig(command=["echo", "event"], events=frozenset(["session.start", "aws.resource.create"]))
    assert hook.matches("session.start") is True
    assert hook.matches("aws.resource.create") is True
    assert hook.matches("session.end") is False

    # Empty events matches all
    catch_all = HookConfig(command=["echo", "all"], events=frozenset())
    assert catch_all.matches("anything") is True


def test_hooks_sanitise_payload() -> None:
    payload = {"tool_name": "list_instances", "tool_output": "x" * 3000}
    sanitised = _sanitise_payload(payload)
    assert len(sanitised["tool_output"]) <= HOOK_TOOL_OUTPUT_LIMIT + 20
    assert "…[truncated]" in sanitised["tool_output"]

    short = {"tool_name": "list_instances", "tool_output": "ok"}
    assert _sanitise_payload(short) == short


@pytest.mark.asyncio
async def test_hooks_load_and_dispatch(tmp_path: Path) -> None:
    hooks_file = tmp_path / "hooks.json"
    hooks_data = {
        "hooks": [
            {
                "command": ["echo", "test"],
                "events": ["session.start"],
            }
        ]
    }
    hooks_file.write_text(json.dumps(hooks_data), encoding="utf-8")

    loaded = load_hooks(hooks_file)
    assert len(loaded) >= 1
    assert loaded[0].command == ["echo", "test"]

    # Dispatch should run without raising errors
    await dispatch_hook("session.start", {"session_id": "test-session"})

    # Fire-and-forget and drain
    dispatch_hook_fire_and_forget("session.start", {"session_id": "bg-session"})
    await drain_pending_hooks()


# ── EventBus Tests ───────────────────────────────────────────────────────────


class TestEventBusValidation:
    def test_validates_prompt_event(self) -> None:
        data = {
            "kind": "prompt",
            "payload": "Review AWS security group",
            "source": "ci",
            "correlation_id": "req-1",
        }
        event, error = EventBus._validate_event(cast(dict[str, Any], data))
        assert error is None
        assert event is not None
        assert event.kind == "prompt"
        assert event.payload == "Review AWS security group"
        assert event.source == "ci"
        assert event.correlation_id == "req-1"
        assert event.bypass == BypassTier.QUEUED

    def test_validates_signal_event_sets_immediate_bypass(self) -> None:
        data = {"kind": "signal", "payload": "interrupt", "source": "watchdog"}
        event, error = EventBus._validate_event(cast(dict[str, Any], data))
        assert error is None
        assert event is not None
        assert event.kind == "signal"
        assert event.payload == "interrupt"
        assert event.bypass == BypassTier.IMMEDIATE

    def test_rejects_invalid_kind(self) -> None:
        data = {"kind": "unsupported", "payload": "foo"}
        event, error = EventBus._validate_event(cast(dict[str, Any], data))
        assert event is None
        assert error is not None and "invalid kind" in error

    def test_rejects_empty_payload(self) -> None:
        data = {"kind": "command", "payload": "   "}
        event, error = EventBus._validate_event(cast(dict[str, Any], data))
        assert event is None
        assert error is not None and "non-empty" in error

    def test_rejects_invalid_signal(self) -> None:
        data = {"kind": "signal", "payload": "terminate"}
        event, error = EventBus._validate_event(cast(dict[str, Any], data))
        assert event is None
        assert error is not None and "invalid signal" in error

    def test_rejects_non_dict(self) -> None:
        event, error = EventBus._validate_event("string-payload")
        assert event is None
        assert error is not None and "expected JSON object" in error


@pytest.mark.asyncio
async def test_event_bus_lifecycle_and_queue() -> None:
    socket_path = Path(f"/tmp/optst_{uuid.uuid4().hex[:8]}.sock")
    bus = EventBus()
    try:
        bound_path = await bus.start(socket_path)
        assert bound_path == socket_path
        assert bus.running is True
        assert bus.socket_path == socket_path

        # Connect client over Unix domain socket
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        msg = {
            "kind": "command",
            "payload": "/compact",
            "source": "cli-tool",
            "correlation_id": "c-100",
        }
        writer.write(json.dumps(msg).encode("utf-8") + b"\n")
        await writer.drain()

        # Check response ACK
        resp_line = await asyncio.wait_for(reader.readline(), timeout=3)
        resp = json.loads(resp_line)
        assert resp["ok"] is True
        assert resp["correlation_id"] == "c-100"

        # Pop from queue
        event = await asyncio.wait_for(bus.get_event(), timeout=3)
        assert event.kind == "command"
        assert event.payload == "/compact"
        assert event.correlation_id == "c-100"

        # No wait on empty queue
        assert bus.get_event_nowait() is None

        writer.close()
        await writer.wait_closed()
    finally:
        await bus.stop()
        assert bus.running is False
        if socket_path.exists():
            socket_path.unlink()


@pytest.mark.asyncio
async def test_event_bus_sink_callback() -> None:
    socket_path = Path(f"/tmp/optst_{uuid.uuid4().hex[:8]}.sock")
    received: list[ExternalEvent] = []

    async def custom_sink(evt: ExternalEvent) -> None:
        received.append(evt)

    bus = EventBus()
    try:
        await bus.start(socket_path, sink=custom_sink)

        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        msg = {"kind": "prompt", "payload": "Terraform validate", "source": "runner"}
        writer.write(json.dumps(msg).encode("utf-8") + b"\n")
        await writer.drain()

        resp_line = await asyncio.wait_for(reader.readline(), timeout=3)
        resp = json.loads(resp_line)
        assert resp["ok"] is True

        assert len(received) == 1
        assert received[0].payload == "Terraform validate"

        writer.close()
        await writer.wait_closed()
    finally:
        await bus.stop()
        if socket_path.exists():
            socket_path.unlink()


@pytest.mark.asyncio
async def test_event_bus_error_responses() -> None:
    socket_path = Path(f"/tmp/optst_{uuid.uuid4().hex[:8]}.sock")
    bus = EventBus()
    try:
        await bus.start(socket_path)

        reader, writer = await asyncio.open_unix_connection(str(socket_path))

        # 1. Invalid JSON
        writer.write(b"{broken json\n")
        await writer.drain()
        resp1 = json.loads(await asyncio.wait_for(reader.readline(), timeout=3))
        assert resp1["ok"] is False
        assert "invalid JSON" in resp1["error"]

        # 2. Validation failure with correlation_id
        writer.write(json.dumps({"kind": "bad", "correlation_id": "cor-err"}).encode() + b"\n")
        await writer.drain()
        resp2 = json.loads(await asyncio.wait_for(reader.readline(), timeout=3))
        assert resp2["ok"] is False
        assert resp2["correlation_id"] == "cor-err"

        writer.close()
        await writer.wait_closed()
    finally:
        await bus.stop()
        if socket_path.exists():
            socket_path.unlink()


def test_default_unix_socket_path() -> None:
    path = default_unix_socket_path()
    assert isinstance(path, Path)
    assert "sock" in str(path)


# ── Notifications Tests ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_notifications_wrappers() -> None:
    with patch("opscloud.integrations.notifications.dispatch_hook", new_callable=AsyncMock) as mock_dispatch:
        await notify_session_start("s-123", "gemini-2.5-pro")
        mock_dispatch.assert_called_once_with(
            "session.start",
            {"session_id": "s-123", "model": "gemini-2.5-pro"},
        )

    with patch("opscloud.integrations.notifications.dispatch_hook", new_callable=AsyncMock) as mock_dispatch:
        await notify_session_end("s-123", 42.5)
        mock_dispatch.assert_called_once_with(
            "session.end",
            {"session_id": "s-123", "duration_s": 42.5},
        )

    with patch("opscloud.integrations.notifications.dispatch_hook", new_callable=AsyncMock) as mock_dispatch:
        await notify_task_complete("Finished provisioning ECS cluster")
        mock_dispatch.assert_called_once_with(
            "task.complete",
            {"summary": "Finished provisioning ECS cluster"},
        )

    with patch("opscloud.integrations.notifications.dispatch_hook_fire_and_forget") as mock_ff:
        notify_tool_use("aws_ec2_describe", "call-1", {"InstanceIds": ["i-1"]})
        mock_ff.assert_called_once_with(
            "tool.use",
            {
                "tool_name": "aws_ec2_describe",
                "tool_id": "call-1",
                "tool_args": {"InstanceIds": ["i-1"]},
            },
        )

    with patch("opscloud.integrations.notifications.dispatch_hook_fire_and_forget") as mock_ff:
        notify_tool_result("aws_ec2_describe", "success", "[]")
        mock_ff.assert_called_once_with(
            "tool.result",
            {
                "tool_name": "aws_ec2_describe",
                "tool_status": "success",
                "tool_output": "[]",
            },
        )

    with patch("opscloud.integrations.notifications.dispatch_hook_fire_and_forget") as mock_ff:
        notify_deploy_event("terraform.apply", {"workspace": "prod"})
        mock_ff.assert_called_once_with("terraform.apply", {"workspace": "prod"})

    with patch("opscloud.integrations.notifications.dispatch_hook_fire_and_forget") as mock_ff:
        notify_cloud_event("aws.resource.create", {"arn": "arn:aws:s3:::my-bucket"})
        mock_ff.assert_called_once_with("aws.resource.create", {"arn": "arn:aws:s3:::my-bucket"})


# ── IdentityMapper Tests ─────────────────────────────────────────────────────


def test_identity_mapper_defaults_and_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # 1. Default unmapped user
    monkeypatch.delenv("IDENTITY_MAPPING_FILE", raising=False)
    monkeypatch.delenv("IDENTITY_MAPPING", raising=False)

    mapper = IdentityMapper()
    identity = mapper.resolve("terminal", "developer")
    assert identity.platform == "terminal"
    assert identity.platform_user_id == "developer"
    assert identity.rbac_role == "viewer"
    assert identity.allowed_accounts == ["*"]
    assert identity.allowed_regions == ["*"]
    assert identity.allowed_services == ["*"]
    assert identity.allowed_operations == ["read"]
    assert mapper.is_mapped("terminal", "developer") is False

    # 2. File mapping
    mapping_data = {
        "terminal:alice": {
            "internal_id": "alice@company.com",
            "display_name": "Alice Admin",
            "rbac_role": "admin",
            "accounts": ["123456789012"],
            "regions": ["us-east-1", "us-west-2"],
            "services": ["ec2", "s3", "iam"],
            "operations": ["read", "deploy", "delete"],
        }
    }
    mapping_file = tmp_path / "identities.json"
    mapping_file.write_text(json.dumps(mapping_data), encoding="utf-8")
    monkeypatch.setenv("IDENTITY_MAPPING_FILE", str(mapping_file))

    file_mapper = IdentityMapper()
    assert file_mapper.is_mapped("terminal", "alice") is True
    alice = file_mapper.resolve("terminal", "alice")
    assert alice.internal_user_id == "alice@company.com"
    assert alice.display_name == "Alice Admin"
    assert alice.rbac_role == "admin"
    assert alice.allowed_accounts == ["123456789012"]
    assert alice.allowed_regions == ["us-east-1", "us-west-2"]
    assert alice.allowed_services == ["ec2", "s3", "iam"]
    assert "delete" in alice.allowed_operations


# ── Stream Bridge & Tool Humanization Tests ──────────────────────────────────


def test_stream_bridge_humanize_tool_name() -> None:
    # AWS tool prefixes
    assert _humanize_tool_name("aws_describe_instances") == "Describe Instances"
    assert _humanize_tool_name("aws_create_bucket") == "Create Bucket"

    # Terraform & OpenTofu
    assert _humanize_tool_name("terraform_apply") == "Apply"
    assert _humanize_tool_name("tofu_plan") == "Plan"

    # Kubernetes & Helm
    assert _humanize_tool_name("k8s_get_pods") == "Get Pods"
    assert _humanize_tool_name("helm_install") == "Install"

    # Transfer & General
    assert _humanize_tool_name("transfer_to_aws_operator") == "Aws Operator"
    assert _humanize_tool_name("custom_inspect_health") == "Custom Inspect Health"
    assert _humanize_tool_name("") == "Tool"


@pytest.mark.asyncio
async def test_stream_bridge_processing() -> None:
    class DummySink:
        def __init__(self) -> None:
            self.started = False
            self.tokens: list[str] = []
            self.ended = False

        async def on_stream_start(self, channel_id: str, thread_id: str) -> None:
            self.started = True

        async def on_token(self, token: str) -> None:
            self.tokens.append(token)

        async def on_thinking(self, text: str) -> None:
            pass

        async def on_tool_call_started(self, name: str, call_id: str, args: dict[str, Any]) -> None:
            pass

        async def on_tool_call_completed(self, name: str, call_id: str, result: str) -> None:
            pass

        async def on_interrupt(self, interrupt_value: dict[str, Any]) -> None:
            pass

        async def on_subagent_event(self, agent_name: str, status: str) -> None:
            pass

        async def on_rubric_event(self, data: dict[str, Any]) -> None:
            pass

        async def on_auto_mode_event(self, data: dict[str, Any]) -> None:
            pass

        async def on_stream_end(self) -> None:
            self.ended = True

        async def on_error(self, error: Exception) -> None:
            pass

        async def on_pre_interrupt(self) -> None:
            pass

    sink = DummySink()
    bridge = GraphStreamBridge(cast(StreamSink, sink))

    from langchain_core.messages import AIMessageChunk

    async def sample_stream():
        yield ("messages", (AIMessageChunk(content="Hello "), {}))
        yield ("messages", (AIMessageChunk(content="Cloud!"), {}))

    full_text = await bridge.process_stream(sample_stream())
    assert full_text == "Hello Cloud!"
    assert sink.started is True
    assert sink.ended is True
    assert sink.tokens == ["Hello ", "Cloud!"]


def test_base_dataclasses() -> None:
    incoming = IncomingMessage(
        text="deploy app",
        user_id="u-1",
        thread_id="t-1",
        channel_id="c-1",
        platform="terminal",
    )
    assert incoming.text == "deploy app"
    assert incoming.platform == "terminal"

    interaction = InteractionPayload(
        action_id="approve",
        user_id="u-1",
        thread_id="t-1",
        channel_id="c-1",
        platform="terminal",
        value="yes",
    )
    assert interaction.action_id == "approve"
    assert interaction.value == "yes"
