"""Comprehensive unit tests for OpsCloud state channels, cloud context, and sessions."""

import asyncio
from pathlib import Path
from typing import Any, get_type_hints
import pytest

from langchain.agents.middleware.types import PrivateStateAttr
from langchain_core.messages import AIMessage, HumanMessage

from opscloud.middleware.resume_state import ResumeStateMiddleware
from opscloud.state.base import AgentState, BaseAgentState, OpsCloudAgentState
from opscloud.state.cloud_context import (
    CloudContextState,
    CloudEnvironmentContext,
    CloudResourceRef,
    _merge_cloud_context,
    _merge_cloud_resources,
    coerce_cloud_provider,
    format_cloud_badge,
    make_resource_ref,
)
from opscloud.state.goal_channels import (
    INHERIT_RUBRIC_MODEL,
    GoalRubricChannels,
    coerce_goal_proposal_kind,
    coerce_goal_status,
    coerce_model_spec,
)
from opscloud.state.resume_state import ResumeState
from opscloud.state.session import (
    SessionManager,
    _count_messages_from_deltas,
    _visible_message_count,
    delete_thread,
    find_similar_threads,
    format_relative_timestamp,
    format_timestamp,
    generate_thread_id,
    get_checkpointer,
    get_db_path,
    list_threads,
    prewarm_thread_message_counts,
    set_db_path,
    thread_exists,
)
from opscloud.state.service_context import (
    ManagedService,
    ServiceContextState,
    ServiceOperationRecord,
    _merge_managed_services,
    _merge_operations_log,
    _merge_plugin_state,
    _merge_unique_strings,
    make_operation_record,
    make_service_record,
)
from opscloud.state.state_migration import migrate_legacy_state


# ── 1. OpsCloudAgentState Schema Structure ────────────────────────────────────

def test_opscloud_agent_state_inheritance():
    """Verify OpsCloudAgentState composes all required channels."""
    bases = getattr(OpsCloudAgentState, "__orig_bases__", ())
    assert CloudContextState in bases
    assert ServiceContextState in bases
    assert ResumeState in bases
    assert GoalRubricChannels in bases
    assert AgentState is OpsCloudAgentState
    assert BaseAgentState is OpsCloudAgentState


def test_private_state_channel_annotations():
    """Verify operational channels carry PrivateStateAttr."""
    hints = get_type_hints(OpsCloudAgentState, include_extras=True)
    expected_private_channels = [
        "_active_cloud_provider",
        "_cloud_context",
        "_cloud_resources_touched",
        "_dry_run",
        "_is_production_environment",
        "_managed_services",
        "_active_service_id",
        "_service_operations_log",
        "_plugin_state",
        "_active_plugins",
        "_context_tokens",
        "_model_spec",
        "_model_params",
        "_goal_objective",
        "_goal_status",
        "_rubric_model_spec",
        "_session_cost_usd",
    ]
    for channel in expected_private_channels:
        assert channel in hints, f"Missing channel {channel}"
        metadata = getattr(hints[channel], "__metadata__", ())
        assert any(
            isinstance(m, type) and issubclass(m, PrivateStateAttr) or m is PrivateStateAttr
            for m in metadata
        ), f"Channel {channel} is missing PrivateStateAttr marker"


# ── Service Management & Plugin State Reducers ────────────────────────────────

def test_service_context_reducers():
    # Managed services merge
    svc1 = make_service_record("svc-1", "payments", "aws", "ecs_service", status="healthy")
    svc2 = make_service_record("svc-2", "auth", "k8s", "deployment", status="degraded")
    merged = _merge_managed_services(None, {"svc-1": svc1})
    assert len(merged) == 1
    assert merged["svc-1"]["status"] == "healthy"

    # Update svc1 health
    update_svc1 = {"status": "updating", "health_summary": "rolling out v2"}
    merged = _merge_managed_services(merged, {"svc-1": update_svc1, "svc-2": svc2})  # type: ignore
    assert len(merged) == 2
    assert merged["svc-1"]["status"] == "updating"
    assert merged["svc-1"]["health_summary"] == "rolling out v2"
    assert merged["svc-2"]["name"] == "auth"

    # Operations log append
    op1 = make_operation_record("op-1", "svc-1", "restart", details="rolling restart triggered")
    op2 = make_operation_record("op-2", "svc-2", "scale", details="scaled from 2 to 5")
    ops = _merge_operations_log(None, [op1])
    assert len(ops) == 1
    # Adding op1 again deduplicates by operation_id
    ops = _merge_operations_log(ops, [op1, op2])
    assert len(ops) == 2
    assert ops[1]["action"] == "scale"

    # Plugin state deep merge
    p_initial = {"aws-ecs": {"cluster": "main", "retries": 3}}
    p_update = {"aws-ecs": {"retries": 5}, "k8s-operator": {"ns": "default"}}
    p_merged = _merge_plugin_state(p_initial, p_update)
    assert p_merged["aws-ecs"]["cluster"] == "main"
    assert p_merged["aws-ecs"]["retries"] == 5
    assert p_merged["k8s-operator"]["ns"] == "default"

    # Active plugins unique accumulation
    plugins = _merge_unique_strings(["aws-core"], ["aws-core", "k8s-ops"])
    assert plugins == ["aws-core", "k8s-ops"]


# ── 2. Cloud Context & Operational State ──────────────────────────────────────

def test_cloud_provider_coercion():
    assert coerce_cloud_provider("aws") == "aws"
    assert coerce_cloud_provider("kubernetes") == "kubernetes"
    assert coerce_cloud_provider("azure") == "azure"
    assert coerce_cloud_provider("gcp") == "gcp"
    assert coerce_cloud_provider("terraform") == "terraform"
    assert coerce_cloud_provider("invalid-cloud") is None
    assert coerce_cloud_provider(123) is None


def test_format_cloud_badge():
    aws_ctx: CloudEnvironmentContext = {
        "active_provider": "aws",
        "aws": {"profile": "prod-infra", "region": "us-west-2"},
    }
    assert format_cloud_badge(aws_ctx) == "[AWS prod-infra]"

    k8s_ctx: CloudEnvironmentContext = {
        "active_provider": "kubernetes",
        "k8s": {"current_context": "eks-cluster-1", "namespace": "payments"},
    }
    assert format_cloud_badge(k8s_ctx) == "[K8s eks-cluster-1/payments]"

    azure_ctx: CloudEnvironmentContext = {
        "active_provider": "azure",
        "azure": {"resource_group": "rg-network-prod"},
    }
    assert format_cloud_badge(azure_ctx) == "[Azure rg-network-prod]"

    tf_ctx: CloudEnvironmentContext = {
        "active_provider": "terraform",
        "iac": {"workspace": "staging", "tool": "terraform"},
    }
    assert format_cloud_badge(tf_ctx) == "[TF staging]"

    assert format_cloud_badge(None) == ""


def test_merge_cloud_context_reducer():
    initial: CloudEnvironmentContext = {
        "active_provider": "aws",
        "aws": {"region": "us-east-1", "profile": "default"},
        "is_production": False,
    }
    update: CloudEnvironmentContext = {
        "active_provider": "aws",
        "aws": {"account_id": "123456789012"},
        "is_production": True,
    }
    merged = _merge_cloud_context(initial, update)
    assert merged is not None
    assert merged["aws"]["region"] == "us-east-1"
    assert merged["aws"]["profile"] == "default"
    assert merged["aws"]["account_id"] == "123456789012"
    assert merged["is_production"] is True


def test_merge_cloud_resources_reducer():
    r1 = make_resource_ref("aws", "s3_bucket", "my-data-bucket", "created")
    r2 = make_resource_ref("aws", "iam_role", "app-role", "created")
    r3 = make_resource_ref("aws", "s3_bucket", "my-data-bucket", "modified")

    # Add r1, r2
    step1 = _merge_cloud_resources(None, [r1, r2])
    assert len(step1) == 2

    # Duplicate r1 should not be duplicated
    step2 = _merge_cloud_resources(step1, [r1])
    assert len(step2) == 2

    # Modified r1 action is distinct from created
    step3 = _merge_cloud_resources(step2, [r3])
    assert len(step3) == 3


# ── 3. Goal & Resume State Coercion ───────────────────────────────────────────

def test_goal_and_resume_coercion():
    assert coerce_goal_status("active") == "active"
    assert coerce_goal_status("paused") == "paused"
    assert coerce_goal_status("invalid") is None

    assert coerce_goal_proposal_kind("create") == "create"
    assert coerce_goal_proposal_kind("amend") == "amend"
    assert coerce_goal_proposal_kind("delete") is None

    assert coerce_model_spec("  openai:gpt-4o  ") == "openai:gpt-4o"
    assert coerce_model_spec("") is None
    assert coerce_model_spec(None) is None
    assert INHERIT_RUBRIC_MODEL == "__opscloud_inherit_rubric__"


# ── 4. ResumeStateMiddleware Context Token Extraction ─────────────────────────

def test_resume_state_middleware_after_model():
    mw = ResumeStateMiddleware()
    msg = AIMessage(
        content="I have provisioned the requested S3 bucket.",
        usage_metadata={"input_tokens": 120, "output_tokens": 45, "total_tokens": 165},
    )
    state: ResumeState = {"messages": [msg]}
    result = mw.after_model(state, runtime=None)  # type: ignore
    assert result is not None
    assert result.get("_context_tokens") == 165


# ── 5. Delta Message Counting ─────────────────────────────────────────────────

def test_delta_message_counting():
    m1 = HumanMessage(content="Hello")
    m2 = AIMessage(content="Hi there")
    deltas = [[m1], [m2]]
    count = _count_messages_from_deltas(deltas)
    assert count == 2


# ── 6. Session Persistence & Checkpointing Lifecycle ──────────────────────────

@pytest.mark.asyncio
async def test_session_lifecycle(tmp_path: Path):
    test_db = tmp_path / "test_sessions.db"
    set_db_path(test_db)

    # Empty initially
    threads = await list_threads()
    assert len(threads) == 0

    tid = generate_thread_id()
    assert len(tid) > 0

    # Ensure checkpointer initializes schema
    async with get_checkpointer() as saver:
        await saver.setup()

    assert await thread_exists(tid) is False

    # Simulate deleting non-existent thread
    deleted = await delete_thread(tid)
    assert deleted is False

    # Test thread similarity search
    assert await find_similar_threads("non-existent") == []

    # Prewarm counts does not crash on empty/initialized DB
    await prewarm_thread_message_counts(limit=10)


# ── 7. State Migration ────────────────────────────────────────────────────────

def test_state_migration(tmp_path: Path):
    legacy_dir = tmp_path / "legacy_opscloud"
    legacy_dir.mkdir()
    state_dir = tmp_path / ".state"

    # Create dummy legacy sessions.db
    legacy_db = legacy_dir / "sessions.db"
    legacy_db.write_text("dummy-sqlite")

    migrate_legacy_state(config_dir=legacy_dir, state_dir=state_dir)

    # File should have moved
    assert not legacy_db.exists()
    assert (state_dir / "sessions.db").exists()
    assert (state_dir / "sessions.db").read_text() == "dummy-sqlite"

    # Running again is idempotent
    migrate_legacy_state(config_dir=legacy_dir, state_dir=state_dir)
    assert (state_dir / "sessions.db").read_text() == "dummy-sqlite"
