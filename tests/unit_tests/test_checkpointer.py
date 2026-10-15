"""Unit tests for OpsCloudCheckpointer extended capabilities."""

import os
from typing import Any, cast
import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import Checkpoint, CheckpointMetadata, empty_checkpoint
from opscloud.state.session import OpsCloudCheckpointer, get_checkpointer


def _make_checkpoint(cp_id: str, channel_values: dict[str, Any] | None = None) -> Checkpoint:
    """Helper to create a fully-typed Checkpoint for unit tests."""
    cp = empty_checkpoint()
    cp["id"] = cp_id
    if channel_values:
        cp["channel_values"] = channel_values
    return cp


@pytest.mark.asyncio
async def test_checkpointer_capabilities_detection():
    """Verify that LangGraph API detects all required extended capabilities on OpsCloudCheckpointer."""
    os.environ["REDIS_URI"] = ""
    from langgraph_api._checkpointer._adapter import CheckpointerCapabilities

    async with get_checkpointer() as saver:
        caps = CheckpointerCapabilities.from_type(type(saver))
        assert caps.has_aget_iter is True
        assert caps.has_adelete_thread is True
        assert caps.has_adelete_for_runs is True
        assert caps.has_acopy_thread is True
        assert caps.has_aprune is True


@pytest.mark.asyncio
async def test_checkpointer_adelete_for_runs():
    """Verify adelete_for_runs removes checkpoints and writes for cancelled/rolled-back runs."""
    async with OpsCloudCheckpointer.from_conn_string(":memory:") as saver:
        await saver.setup()
        cfg1: RunnableConfig = {"configurable": {"thread_id": "thread-1", "checkpoint_ns": "", "run_id": "run-alpha"}}
        cp1 = _make_checkpoint("cp-1")
        meta1: CheckpointMetadata = {"run_id": "run-alpha"}
        await saver.aput(cfg1, cp1, meta1, {})

        cfg2: RunnableConfig = {"configurable": {"thread_id": "thread-1", "checkpoint_ns": "", "run_id": "run-beta"}}
        cp2 = _make_checkpoint("cp-2")
        meta2: CheckpointMetadata = {"run_id": "run-beta"}
        await saver.aput(cfg2, cp2, meta2, {})

        # Delete run-alpha
        await saver.adelete_for_runs(["run-alpha"])

        tup1_cfg: RunnableConfig = {"configurable": {"thread_id": "thread-1", "checkpoint_id": "cp-1"}}
        tup2_cfg: RunnableConfig = {"configurable": {"thread_id": "thread-1", "checkpoint_id": "cp-2"}}
        tup1 = await saver.aget_tuple(tup1_cfg)
        tup2 = await saver.aget_tuple(tup2_cfg)

        assert tup1 is None, "Run alpha checkpoint must be deleted"
        assert tup2 is not None, "Run beta checkpoint must remain intact"


@pytest.mark.asyncio
async def test_checkpointer_acopy_thread():
    """Verify acopy_thread duplicates all checkpoints and writes to target thread."""
    async with OpsCloudCheckpointer.from_conn_string(":memory:") as saver:
        await saver.setup()
        cfg: RunnableConfig = {"configurable": {"thread_id": "src-thread", "checkpoint_ns": "", "checkpoint_id": "cp-orig"}}
        cp = _make_checkpoint("cp-orig", channel_values={"msg": "hi"})
        stored_cfg = await saver.aput(cfg, cp, cast(CheckpointMetadata, {"thread_id": "src-thread"}), {})
        await saver.aput_writes(stored_cfg, [("msg", "reply")], task_id="task-1")

        # Copy to dst-thread
        await saver.acopy_thread("src-thread", "dst-thread")

        dst_cfg: RunnableConfig = {"configurable": {"thread_id": "dst-thread", "checkpoint_id": "cp-orig"}}
        dst_tup = await saver.aget_tuple(dst_cfg)
        assert dst_tup is not None
        assert dst_tup.config.get("configurable", {}).get("thread_id") == "dst-thread"
        assert dst_tup.pending_writes is not None
        assert len(dst_tup.pending_writes) == 1
        assert dst_tup.pending_writes[0][1] == "msg"


@pytest.mark.asyncio
async def test_checkpointer_aprune():
    """Verify aprune with keep_latest and delete_all strategies."""
    async with OpsCloudCheckpointer.from_conn_string(":memory:") as saver:
        await saver.setup()
        for i in range(1, 4):
            cp_id = f"cp-{i:03d}"
            cp = _make_checkpoint(cp_id)
            step_cfg: RunnableConfig = {
                "configurable": {
                    "thread_id": "thread-prune",
                    "checkpoint_ns": "",
                    "checkpoint_id": cp_id,
                }
            }
            await saver.aput(step_cfg, cp, cast(CheckpointMetadata, {}), {})

        # Prune keep_latest
        await saver.aprune(["thread-prune"], strategy="keep_latest")

        c1_cfg: RunnableConfig = {"configurable": {"thread_id": "thread-prune", "checkpoint_ns": "", "checkpoint_id": "cp-001"}}
        c2_cfg: RunnableConfig = {"configurable": {"thread_id": "thread-prune", "checkpoint_ns": "", "checkpoint_id": "cp-002"}}
        c3_cfg: RunnableConfig = {"configurable": {"thread_id": "thread-prune", "checkpoint_ns": "", "checkpoint_id": "cp-003"}}

        assert await saver.aget_tuple(c1_cfg) is None
        assert await saver.aget_tuple(c2_cfg) is None
        latest = await saver.aget_tuple(c3_cfg)
        assert latest is not None

        # Prune delete_all
        await saver.aprune(["thread-prune"], strategy="delete_all")
        assert await saver.aget_tuple(c3_cfg) is None


@pytest.mark.asyncio
async def test_checkpointer_aprune_subagents_and_vacuum():
    """Verify aprune drops subagent namespaces and avacuum works."""
    async with OpsCloudCheckpointer.from_conn_string(":memory:") as saver:
        await saver.setup()
        main_cfg: RunnableConfig = {
            "configurable": {
                "thread_id": "thread-sub",
                "checkpoint_ns": "",
                "checkpoint_id": "cp-main-001",
            }
        }
        sub_cfg: RunnableConfig = {
            "configurable": {
                "thread_id": "thread-sub",
                "checkpoint_ns": "tools:call_123",
                "checkpoint_id": "cp-sub-001",
            }
        }
        await saver.aput(main_cfg, _make_checkpoint("cp-main-001"), cast(CheckpointMetadata, {}), {})
        await saver.aput(sub_cfg, _make_checkpoint("cp-sub-001"), cast(CheckpointMetadata, {}), {})

        assert await saver.aget_tuple(sub_cfg) is not None

        await saver.aprune(["thread-sub"], strategy="keep_latest", prune_subagents=True)

        assert await saver.aget_tuple(main_cfg) is not None
        assert await saver.aget_tuple(sub_cfg) is None

        # Verify avacuum executes cleanly
        await saver.avacuum()


@pytest.mark.asyncio
async def test_checkpointer_aprune_preserves_interrupt_checkpoints():
    """Verify aprune preserves non-terminal checkpoints containing pending HITL interrupts (IA-04)."""
    async with OpsCloudCheckpointer.from_conn_string(":memory:") as saver:
        await saver.setup()
        # Create cp-001 (historical regular)
        cfg1: RunnableConfig = {"configurable": {"thread_id": "thread-hitl", "checkpoint_ns": "", "checkpoint_id": "cp-001"}}
        await saver.aput(cfg1, _make_checkpoint("cp-001"), cast(CheckpointMetadata, {}), {})

        # Create cp-002 (interrupted HITL checkpoint with __interrupt__ write)
        cfg2: RunnableConfig = {"configurable": {"thread_id": "thread-hitl", "checkpoint_ns": "", "checkpoint_id": "cp-002"}}
        stored_cfg2 = await saver.aput(cfg2, _make_checkpoint("cp-002"), cast(CheckpointMetadata, {}), {})
        await saver.aput_writes(stored_cfg2, [("__interrupt__", {"approval_requested": True})], task_id="task-hitl")

        # Create cp-003 (latest regular)
        cfg3: RunnableConfig = {"configurable": {"thread_id": "thread-hitl", "checkpoint_ns": "", "checkpoint_id": "cp-003"}}
        await saver.aput(cfg3, _make_checkpoint("cp-003"), cast(CheckpointMetadata, {}), {})

        # Compact thread
        await saver.aprune(["thread-hitl"], strategy="keep_latest")

        # cp-001 must be pruned (historical regular)
        assert await saver.aget_tuple(cfg1) is None
        # cp-002 MUST be preserved (contains pending HITL interrupt)
        tup2 = await saver.aget_tuple(cfg2)
        assert tup2 is not None
        assert tup2.pending_writes is not None
        assert tup2.pending_writes[0][1] == "__interrupt__"
        # cp-003 must be preserved (latest checkpoint)
        assert await saver.aget_tuple(cfg3) is not None


@pytest.mark.asyncio
async def test_checkpointer_aprune_preserves_subagent_interrupt_checkpoints():
    """Verify aprune preserves subagent checkpoints that contain active HITL interrupts (DV-01)."""
    async with OpsCloudCheckpointer.from_conn_string(":memory:") as saver:
        await saver.setup()
        # 1. Main agent checkpoint
        main_cfg: RunnableConfig = {"configurable": {"thread_id": "thread-sub-hitl", "checkpoint_ns": "", "checkpoint_id": "cp-main"}}
        await saver.aput(main_cfg, _make_checkpoint("cp-main"), cast(CheckpointMetadata, {}), {})

        # 2. Regular completed subagent (should be pruned)
        sub_dead_cfg: RunnableConfig = {"configurable": {"thread_id": "thread-sub-hitl", "checkpoint_ns": "tools:call_dead", "checkpoint_id": "cp-sub-dead"}}
        await saver.aput(sub_dead_cfg, _make_checkpoint("cp-sub-dead"), cast(CheckpointMetadata, {}), {})

        # 3. Interrupted subagent with pending __interrupt__ write (MUST be preserved)
        sub_hitl_cfg: RunnableConfig = {"configurable": {"thread_id": "thread-sub-hitl", "checkpoint_ns": "tools:call_active_hitl", "checkpoint_id": "cp-sub-hitl"}}
        stored_sub = await saver.aput(sub_hitl_cfg, _make_checkpoint("cp-sub-hitl"), cast(CheckpointMetadata, {}), {})
        await saver.aput_writes(stored_sub, [("__interrupt__", {"tool_approval": "terraform apply"})], task_id="task-sub-1")

        # 4. Another interrupted subagent under subagent:ns
        sub_agent_cfg: RunnableConfig = {"configurable": {"thread_id": "thread-sub-hitl", "checkpoint_ns": "subagent:cloud_sec", "checkpoint_id": "cp-subagent-hitl"}}
        stored_agent = await saver.aput(sub_agent_cfg, _make_checkpoint("cp-subagent-hitl"), cast(CheckpointMetadata, {}), {})
        await saver.aput_writes(stored_agent, [("__interrupt__", {"escalation_needed": True})], task_id="task-sub-2")

        # Prune with prune_subagents=True
        await saver.aprune(["thread-sub-hitl"], strategy="keep_latest", prune_subagents=True)

        # Main checkpoint preserved
        assert await saver.aget_tuple(main_cfg) is not None
        # Dead subagent was pruned
        assert await saver.aget_tuple(sub_dead_cfg) is None
        # Interrupted subagent in tools:* MUST be preserved
        sub_tup = await saver.aget_tuple(sub_hitl_cfg)
        assert sub_tup is not None
        assert sub_tup.pending_writes is not None
        assert sub_tup.pending_writes[0][1] == "__interrupt__"
        # Interrupted subagent in subagent:* MUST be preserved
        agent_tup = await saver.aget_tuple(sub_agent_cfg)
        assert agent_tup is not None
        assert agent_tup.pending_writes is not None
        assert agent_tup.pending_writes[0][1] == "__interrupt__"


@pytest.mark.asyncio
async def test_session_manager_maintain_database_lease_contention(tmp_path):
    """Verify maintain_database respects size threshold and skips when lease is held (DV-08)."""
    import fcntl
    import sys
    from opscloud.state.session import SessionManager

    db_path = tmp_path / "sessions.db"
    # Create empty DB file
    db_path.write_bytes(b"0" * 1024)

    # 1. Below threshold
    res = await SessionManager.maintain_database(db_path=db_path, size_threshold_mb=10.0)
    assert res["performed"] is False
    assert res["reason"] == "below_threshold"

    if sys.platform != "win32":
        # Simulate active lease held by another process
        lock_file = db_path.with_name(db_path.name + ".maint_lock")
        with open(lock_file, "w") as lock_fp:
            fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # 2. Above threshold (size_threshold_mb=0) but lease held -> must report lease_busy
            busy_res = await SessionManager.maintain_database(db_path=db_path, size_threshold_mb=0.0)
            assert busy_res["performed"] is False
            assert busy_res["reason"] == "lease_busy"
            fcntl.flock(lock_fp, fcntl.LOCK_UN)


@pytest.mark.asyncio
async def test_checkpointer_aprune_agent_session_namespaces_and_resolved_interrupts():
    """Verify aprune drops AgentSessionMiddleware namespaces and resolved HITL interrupts."""
    async with OpsCloudCheckpointer.from_conn_string(":memory:") as saver:
        await saver.setup()
        # 1. Historical root checkpoint with a RESOLVED interrupt (__interrupt__ + __resume__)
        cfg_resolved_hitl: RunnableConfig = {
            "configurable": {
                "thread_id": "thread-bloat",
                "checkpoint_ns": "",
                "checkpoint_id": "cp-root-001",
            }
        }
        stored_root_1 = await saver.aput(cfg_resolved_hitl, _make_checkpoint("cp-root-001"), cast(CheckpointMetadata, {}), {})
        await saver.aput_writes(stored_root_1, [("__interrupt__", {"approval": True})], task_id="task-hitl")
        await saver.aput_writes(stored_root_1, [("__resume__", {"type": "approve"})], task_id="task-resume")

        # 2. Historical root checkpoint with a RESOLVED interrupt via branch:to:*
        cfg_branched_hitl: RunnableConfig = {
            "configurable": {
                "thread_id": "thread-bloat",
                "checkpoint_ns": "",
                "checkpoint_id": "cp-root-002",
            }
        }
        stored_root_2 = await saver.aput(cfg_branched_hitl, _make_checkpoint("cp-root-002"), cast(CheckpointMetadata, {}), {})
        await saver.aput_writes(stored_root_2, [("__interrupt__", {"approval": True})], task_id="task-hitl-2")
        await saver.aput_writes(stored_root_2, [("branch:to:model", None)], task_id="task-branch-2")

        # 3. Latest root checkpoint
        cfg_latest: RunnableConfig = {
            "configurable": {
                "thread_id": "thread-bloat",
                "checkpoint_ns": "",
                "checkpoint_id": "cp-root-003",
            }
        }
        await saver.aput(cfg_latest, _make_checkpoint("cp-root-003"), cast(CheckpointMetadata, {}), {})

        # 4. Orphaned AgentSessionMiddleware subgraph checkpoint
        cfg_agent_ns: RunnableConfig = {
            "configurable": {
                "thread_id": "thread-bloat",
                "checkpoint_ns": "AgentSessionMiddleware.before_agent:abc123",
                "checkpoint_id": "cp-agent-001",
            }
        }
        await saver.aput(cfg_agent_ns, _make_checkpoint("cp-agent-001"), cast(CheckpointMetadata, {}), {})

        await saver.aprune(["thread-bloat"], strategy="keep_latest", prune_subagents=True)

        # Resolved interrupt checkpoints must be pruned
        assert await saver.aget_tuple(cfg_resolved_hitl) is None
        assert await saver.aget_tuple(cfg_branched_hitl) is None
        # Latest root checkpoint must remain
        assert await saver.aget_tuple(cfg_latest) is not None
        # Orphaned AgentSessionMiddleware namespace must be pruned
        assert await saver.aget_tuple(cfg_agent_ns) is None

