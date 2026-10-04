"""Unit tests for OpsCloudCheckpointer extended capabilities."""

import os
import pytest
from langgraph.checkpoint.base import Checkpoint
from opscloud.state.session import OpsCloudCheckpointer, get_checkpointer


@pytest.mark.asyncio
async def test_checkpointer_capabilities_detection():
    """Verify that LangGraph API detects all required extended capabilities on OpsCloudCheckpointer."""
    os.environ["REDIS_URI"] = ""
    from langgraph_api._checkpointer._adapter import CheckpointerCapabilities

    async with get_checkpointer() as saver:
        caps = CheckpointerCapabilities.from_type(type(saver))
        assert caps.has_adelete_thread is True
        assert caps.has_adelete_for_runs is True
        assert caps.has_acopy_thread is True
        assert caps.has_aprune is True


@pytest.mark.asyncio
async def test_checkpointer_adelete_for_runs():
    """Verify adelete_for_runs removes checkpoints and writes for cancelled/rolled-back runs."""
    async with OpsCloudCheckpointer.from_conn_string(":memory:") as saver:
        await saver.setup()
        cfg1 = {"configurable": {"thread_id": "thread-1", "checkpoint_ns": "", "run_id": "run-alpha"}}
        cp1: Checkpoint = {"v": 1, "id": "cp-1", "ts": "2026-09-30T14:00:00Z", "channel_values": {}, "channel_versions": {}, "versions_seen": {}}
        await saver.aput(cfg1, cp1, {"run_id": "run-alpha"}, {})

        cfg2 = {"configurable": {"thread_id": "thread-1", "checkpoint_ns": "", "run_id": "run-beta"}}
        cp2: Checkpoint = {"v": 1, "id": "cp-2", "ts": "2026-09-30T14:01:00Z", "channel_values": {}, "channel_versions": {}, "versions_seen": {}}
        await saver.aput(cfg2, cp2, {"run_id": "run-beta"}, {})

        # Delete run-alpha
        await saver.adelete_for_runs(["run-alpha"])

        tup1 = await saver.aget_tuple({"configurable": {"thread_id": "thread-1", "checkpoint_id": "cp-1"}})
        tup2 = await saver.aget_tuple({"configurable": {"thread_id": "thread-1", "checkpoint_id": "cp-2"}})

        assert tup1 is None, "Run alpha checkpoint must be deleted"
        assert tup2 is not None, "Run beta checkpoint must remain intact"


@pytest.mark.asyncio
async def test_checkpointer_acopy_thread():
    """Verify acopy_thread duplicates all checkpoints and writes to target thread."""
    async with OpsCloudCheckpointer.from_conn_string(":memory:") as saver:
        await saver.setup()
        cfg = {"configurable": {"thread_id": "src-thread", "checkpoint_ns": "", "checkpoint_id": "cp-orig"}}
        cp: Checkpoint = {"v": 1, "id": "cp-orig", "ts": "2026-09-30T14:00:00Z", "channel_values": {"msg": "hi"}, "channel_versions": {}, "versions_seen": {}}
        stored_cfg = await saver.aput(cfg, cp, {"thread_id": "src-thread"}, {})
        await saver.aput_writes(stored_cfg, [("msg", "reply")], task_id="task-1")

        # Copy to dst-thread
        await saver.acopy_thread("src-thread", "dst-thread")

        dst_tup = await saver.aget_tuple({"configurable": {"thread_id": "dst-thread", "checkpoint_id": "cp-orig"}})
        assert dst_tup is not None
        assert dst_tup.config["configurable"]["thread_id"] == "dst-thread"
        assert len(dst_tup.pending_writes) == 1
        assert dst_tup.pending_writes[0][1] == "msg"


@pytest.mark.asyncio
async def test_checkpointer_aprune():
    """Verify aprune with keep_latest and delete_all strategies."""
    async with OpsCloudCheckpointer.from_conn_string(":memory:") as saver:
        await saver.setup()
        cfg = {"configurable": {"thread_id": "thread-prune", "checkpoint_ns": ""}}
        for i in range(1, 4):
            cp_id = f"cp-{i:03d}"
            cp: Checkpoint = {"v": 1, "id": cp_id, "ts": f"2026-09-30T14:0{i}:00Z", "channel_values": {}, "channel_versions": {}, "versions_seen": {}}
            await saver.aput({**cfg, "configurable": {**cfg["configurable"], "checkpoint_id": cp_id}}, cp, {}, {})

        # Prune keep_latest
        await saver.aprune(["thread-prune"], strategy="keep_latest")

        assert await saver.aget_tuple({**cfg, "configurable": {**cfg["configurable"], "checkpoint_id": "cp-001"}}) is None
        assert await saver.aget_tuple({**cfg, "configurable": {**cfg["configurable"], "checkpoint_id": "cp-002"}}) is None
        latest = await saver.aget_tuple({**cfg, "configurable": {**cfg["configurable"], "checkpoint_id": "cp-003"}})
        assert latest is not None

        # Prune delete_all
        await saver.aprune(["thread-prune"], strategy="delete_all")
        assert await saver.aget_tuple({**cfg, "configurable": {**cfg["configurable"], "checkpoint_id": "cp-003"}}) is None
