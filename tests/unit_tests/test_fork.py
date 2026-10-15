"""Unit tests for ``/fork`` (alias ``/branch``) conversation branching."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage

from opscloud.commands._base import CommandContext
from opscloud.commands._types import BypassTier
from opscloud.commands.core.fork import ForkHandler
from opscloud.commands.core.resume import ResumeHandler
from opscloud.middleware.goal_state_notice import (
    FORK_NOTICE_MESSAGE_SOURCE,
    build_fork_notice_message,
    is_conversation_control_message,
    is_internal_message,
    message_source,
)
from opscloud.state.session import (
    OpsCloudCheckpointer,
    SessionManager,
    set_db_path,
)
from opscloud.ui.command_registry import IMMEDIATE_CMDS, get_command
from opscloud.ui.remote_client import RemoteAgent


def test_fork_command_registration_and_aliases() -> None:
    """Verify /fork and /branch are registered with expected metadata."""
    handler = ForkHandler()
    assert handler.name == "/fork"
    assert handler.aliases == ("/branch",)
    assert handler.bypass_tier == BypassTier.IMMEDIATE
    assert "/fork" in IMMEDIATE_CMDS
    assert "/branch" in IMMEDIATE_CMDS

    fork_desc = get_command("/fork")
    assert fork_desc is not None
    assert fork_desc.name == "/fork"
    assert "/branch" in fork_desc.aliases
    assert get_command("/branch") == fork_desc


def test_build_fork_notice_message_and_internal_classification() -> None:
    """Verify <fork_notice> envelope formatting and internal message classification."""
    msg = build_fork_notice_message(
        source_thread_id="01963000-0000-7000-8000-000000000111",
        new_thread_id="01963000-0000-7000-8000-000000000222",
    )
    assert isinstance(msg, HumanMessage)
    assert isinstance(msg.content, str)
    assert msg.content.startswith("<fork_notice>\n")
    assert msg.content.endswith("\n</fork_notice>")
    assert "01963000-0000-7000-8000-000000000111" in msg.content
    assert "The subagents invoked in the history above are NOT yours" in msg.content
    assert "The background tasks in the history above are NOT yours" in msg.content
    assert message_source(msg) == FORK_NOTICE_MESSAGE_SOURCE
    assert is_internal_message(msg) is True
    assert is_conversation_control_message(msg) is True

    # Even if serialized without additional_kwargs, <fork_notice> prefix is recognized as internal
    raw_serialized = {"role": "human", "content": msg.content}
    assert is_internal_message(raw_serialized) is True
    assert is_conversation_control_message(raw_serialized) is True


@pytest.mark.asyncio
async def test_fork_preconditions() -> None:
    """Verify ForkHandler enforces active-thread, turn-boundary, and subagent preconditions."""
    handler = ForkHandler()

    # 1. No active thread
    ctx_no_thread = CommandContext(app=None, raw_command="/fork", args="", thread_id=None)
    res_no_thread = await handler.execute(ctx_no_thread)
    assert res_no_thread.success is False
    assert res_no_thread.message == "No active conversation to fork."

    # 2. Mid-turn rejection (requireForkAtTurnBoundary)
    mock_app_busy = MagicMock()
    mock_app_busy._agent_thread_id = "thread-active-1"
    mock_app_busy._agent_running = True
    mock_app_busy._shell_running = False
    ctx_busy = CommandContext(
        app=mock_app_busy,
        raw_command="/fork",
        args="",
        thread_id="thread-active-1",
    )
    res_busy = await handler.execute(ctx_busy)
    assert res_busy.success is False
    assert "turn is in progress" in (res_busy.message or "")

    # 3. Subagent conversation rejection
    mock_app_sub = MagicMock()
    mock_app_sub._agent_thread_id = "thread-sub-1"
    mock_app_sub._agent_running = False
    mock_app_sub._shell_running = False
    mock_app_sub._agent_session_state = MagicMock(is_active=True)
    ctx_sub = CommandContext(
        app=mock_app_sub,
        raw_command="/fork",
        args="",
        thread_id="thread-sub-1",
    )
    res_sub = await handler.execute(ctx_sub)
    assert res_sub.success is False
    assert "Cannot fork a subagent conversation" in (res_sub.message or "")


@pytest.mark.asyncio
async def test_remote_agent_afork_thread_copies_and_appends_fork_notice() -> None:
    """Verify RemoteAgent.afork_thread calls client.threads.copy and appends <fork_notice> via aupdate_state."""
    agent = RemoteAgent(url="http://localhost:2024", graph_name="agent")

    source_tid = "01963000-0000-7000-8000-000000000aaa"
    forked_tid = "01963000-0000-7000-8000-000000000bbb"

    mock_client = MagicMock()
    mock_client.threads.copy = AsyncMock(return_value={"thread_id": forked_tid})

    mock_graph = MagicMock()
    mock_graph._validate_client = MagicMock(return_value=mock_client)
    mock_graph.aupdate_state = AsyncMock(return_value={"configurable": {"thread_id": forked_tid}})

    with patch.object(agent, "_get_graph", return_value=mock_graph):
        new_id = await agent.afork_thread(source_tid)

    assert new_id == forked_tid
    mock_client.threads.copy.assert_awaited_once_with(source_tid)
    mock_graph.aupdate_state.assert_awaited_once()

    call_args = mock_graph.aupdate_state.call_args
    prepared_cfg = call_args[0][0]
    state_values = call_args[0][1]
    assert prepared_cfg["configurable"]["thread_id"] == forked_tid
    assert len(state_values["messages"]) == 1
    notice_payload = state_values["messages"][0]
    assert notice_payload["role"] == "human"
    assert "<fork_notice>" in notice_payload["content"]
    assert source_tid in notice_payload["content"]
    assert notice_payload["additional_kwargs"]["lc_source"] == FORK_NOTICE_MESSAGE_SOURCE
    assert notice_payload["additional_kwargs"]["source_thread_id"] == source_tid


@pytest.mark.asyncio
async def test_session_manager_fork_thread_copies_sqlite_checkpoints(tmp_path: Path) -> None:
    """Verify SessionManager.fork_thread duplicates SQLite checkpoints and writes without mutating source."""
    db_file = tmp_path / "fork_test.db"
    set_db_path(db_file)

    source_tid = "01963000-0000-7000-8000-000000000100"
    async with OpsCloudCheckpointer.from_conn_string(str(db_file)) as saver:
        await saver.setup()
        await saver.conn.execute(
            "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                source_tid,
                "",
                "cp-1",
                None,
                "json",
                b'{"v": 1, "channel_values": {"messages": ["hello"]}}',
                f'{{"thread_id": "{source_tid}", "step": 1}}'.encode(),
            ),
        )
        await saver.conn.execute(
            "INSERT INTO writes (thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, value) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (source_tid, "", "cp-1", "task-1", 0, "messages", "json", b'"world"'),
        )
        await saver.conn.commit()

    new_tid = await SessionManager.fork_thread(source_tid)
    assert new_tid != source_tid

    async with OpsCloudCheckpointer.from_conn_string(str(db_file)) as saver:
        async with saver.conn.execute(
            "SELECT checkpoint_id, metadata FROM checkpoints WHERE thread_id = ?",
            (new_tid,),
        ) as cur:
            cp_rows = list(await cur.fetchall())
        assert len(cp_rows) == 1
        assert cp_rows[0][0] == "cp-1"
        assert new_tid.encode() in cp_rows[0][1]

        async with saver.conn.execute(
            "SELECT channel, value FROM writes WHERE thread_id = ?",
            (new_tid,),
        ) as cur:
            w_rows = list(await cur.fetchall())
        assert len(w_rows) == 1
        assert w_rows[0] == ("messages", b'"world"')


@pytest.mark.asyncio
async def test_fork_handler_and_direct_resume_roundtrip() -> None:
    """Verify /fork switches thread in-place and /resume <previous_id> returns to the source thread."""
    fork_handler = ForkHandler()
    resume_handler = ResumeHandler()

    source_tid = "01963000-0000-7000-8000-000000000555"
    branched_tid = "01963000-0000-7000-8000-000000000666"

    mock_app = MagicMock()
    mock_app._agent_thread_id = source_tid
    mock_app._agent_running = False
    mock_app._shell_running = False
    mock_app._agent_session_state = MagicMock(is_active=False)

    async def _fake_fork_thread(*, source_thread_id: str | None = None) -> tuple[str, str]:
        prev = source_thread_id or mock_app._agent_thread_id
        mock_app._previous_thread_id = prev
        mock_app._agent_thread_id = branched_tid
        return prev, branched_tid

    mock_app.fork_thread = AsyncMock(side_effect=_fake_fork_thread)
    mock_app.resume_thread = AsyncMock()

    mock_session = MagicMock()
    mock_session.thread_id = source_tid
    mock_session.thread_exists = AsyncMock(return_value=True)

    ctx_fork = CommandContext(
        app=mock_app,
        session=mock_session,
        raw_command="/fork",
        args="",
        thread_id=source_tid,
    )
    res_fork = await fork_handler.execute(ctx_fork)

    assert res_fork.success is True
    assert res_fork.data["previous_thread_id"] == source_tid
    assert res_fork.data["new_thread_id"] == branched_tid
    assert f"/resume {source_tid}" in (res_fork.message or "")
    assert mock_app._agent_thread_id == branched_tid
    assert mock_session.previous_thread_id == source_tid
    assert mock_session.thread_id == branched_tid

    # Now execute `/resume <source_tid>` directly as instructed by `/fork` output
    ctx_resume = CommandContext(
        app=mock_app,
        session=mock_session,
        raw_command=f"/resume {source_tid}",
        args=source_tid,
        thread_id=branched_tid,
    )
    res_resume = await resume_handler.execute(ctx_resume)
    assert res_resume.success is True
    mock_app.resume_thread.assert_awaited_once_with(source_tid)


@pytest.mark.asyncio
async def test_opscloud_app_fork_thread_in_place_state_update() -> None:
    """Verify OpsCloudApp.fork_thread updates thread IDs and preserves UI state without clearing messages."""
    from opscloud.ui.app import OpsCloudApp

    source_tid = "01963000-0000-7000-8000-000000000777"
    branched_tid = "01963000-0000-7000-8000-000000000888"

    mock_client = MagicMock()
    mock_client.afork_thread = AsyncMock(return_value=branched_tid)

    dummy_app = MagicMock(spec=OpsCloudApp)
    dummy_app._agent_thread_id = source_tid
    dummy_app._client = mock_client
    dummy_app._session_state = MagicMock(thread_id=source_tid, previous_thread_id=None)
    dummy_app._goal_state = MagicMock(thread_id=source_tid)
    dummy_app._agent_session_state = MagicMock(thread_id=source_tid)

    prev_id, new_id = await OpsCloudApp.fork_thread(dummy_app)

    assert prev_id == source_tid
    assert new_id == branched_tid
    assert dummy_app._previous_thread_id == source_tid
    assert dummy_app._agent_thread_id == branched_tid
    assert dummy_app._session_state.previous_thread_id == source_tid
    assert dummy_app._session_state.thread_id == branched_tid
    assert dummy_app._goal_state.thread_id == branched_tid
    assert dummy_app._agent_session_state.thread_id == branched_tid
    mock_client.afork_thread.assert_awaited_once_with(source_tid)


@pytest.mark.asyncio
async def test_remote_agent_afork_thread_fallback_when_sdk_copy_fails(tmp_path: Path) -> None:
    """Verify RemoteAgent.afork_thread falls back to SessionManager.fork_thread + threads.create when threads.copy fails."""
    db_file = tmp_path / "fallback_fork.db"
    set_db_path(db_file)

    agent = RemoteAgent(url="http://localhost:2024", graph_name="agent")
    source_tid = "01963000-0000-7000-8000-000000000900"

    mock_client = MagicMock()
    mock_client.threads.copy = AsyncMock(side_effect=RuntimeError("404 thread not found on server"))
    mock_client.threads.create = AsyncMock(return_value={"thread_id": "created"})

    mock_graph = MagicMock()
    mock_graph._validate_client = MagicMock(return_value=mock_client)
    mock_graph.aupdate_state = AsyncMock(return_value={"configurable": {}})

    with patch.object(agent, "_get_graph", return_value=mock_graph):
        new_id = await agent.afork_thread(source_tid)

    assert isinstance(new_id, str) and len(new_id) > 0 and new_id != source_tid
    mock_client.threads.copy.assert_awaited_once_with(source_tid)
    assert mock_client.threads.create.await_count == 2
    mock_client.threads.create.assert_any_await(
        thread_id=source_tid,
        if_exists="do_nothing",
        metadata=None,
        graph_id="agent",
    )
    mock_client.threads.create.assert_any_await(
        thread_id=new_id,
        if_exists="do_nothing",
        metadata={"forked_from": source_tid},
        graph_id="agent",
    )
    mock_graph.aupdate_state.assert_awaited_once()

