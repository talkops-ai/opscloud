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
