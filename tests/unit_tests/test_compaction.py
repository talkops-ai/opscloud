"""Unit tests for context compaction and lossless dual-JSONL transcripts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from deepagents.backends.protocol import BackendProtocol
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
import pytest

from opscloud.commands._base import CommandContext
from opscloud.commands.core.compact import CompactHandler
from opscloud.config import paths
from opscloud.config.cli_settings import auto_compact_override, parse_settings_fields
from opscloud.middleware.compaction import (
    CONVERSATION_TRANSCRIPT_SYSTEM_SECTION,
    CompactRequestType,
    OpsCloudSummarizationMiddleware,
    _create_cli_compaction_middleware,
    agenerate_llm_continuation_summary,
    build_context_summary_envelope,
    capture_user_requests,
    collect_artifact_snapshots,
    collect_subagent_and_task_snapshots,
    compute_trajectory_file_interactions,
    ensure_not_starting_with_tool_call,
    generate_fallback_summary,
    generate_llm_continuation_summary,
    get_checkpoint_config_for_model_info,
    get_default_checkpoint_config_for_model,
    get_water_fill_allocation,
    middle_truncate_bytes,
    parse_compact_request,
    remove_unpaired_tool_calls_and_results,
    step_to_summary_json,
    sync_messages_to_transcript,
    truncate_and_offload_tool_output,
    validate_checkpoint_config_for_model_info,
)


class _DummyBackend(BackendProtocol):
    pass


def test_auto_compact_override_and_settings_validation() -> None:
    """Verify auto_compact_override percentage and token threshold parsing."""
    assert auto_compact_override("80%") == ("80%", True)
    assert auto_compact_override(" 100% ") == ("100%", True)
    assert auto_compact_override("0%") == ("", False)
    assert auto_compact_override("105%") == ("", False)
    assert auto_compact_override("150000") == ("150000", True)
    assert auto_compact_override("-50") == ("", False)
    assert auto_compact_override("invalid") == ("", False)

    setting, errors = parse_settings_fields({"autoCompact": "75%"})
    assert not errors
    assert setting.auto_compact == "75%"
    assert setting.to_sparse_dict()["autoCompact"] == "75%"

    _, bad_errors = parse_settings_fields({"autoCompact": "150%"})
    assert bad_errors


def test_checkpoint_config_calculation_and_validation() -> None:
    """Verify default checkpoint threshold calculation and validation against model limits."""
    cfg = get_default_checkpoint_config_for_model(200_000, 8_192)
    # reserve = max(8192, 40000) = 40000 -> 200000 - 40000 = 160000
    assert cfg.max_token_limit == 160_000
    assert cfg.max_output_tokens == 8_192
    assert pytest.approx(cfg.overhead_ratio) == 0.15
    assert cfg.max_user_requests == 20
    assert cfg.max_user_request_bytes == 4096

    # With 50% autoCompact override on 200,000 input tokens -> 100,000
    cfg_override = get_checkpoint_config_for_model_info(200_000, 32_000, auto_compact_raw="50%")
    assert cfg_override.max_token_limit == 100_000
    assert cfg_override.max_output_tokens == 16_384

    cfg_invalid = get_default_checkpoint_config_for_model(200_000, 8_192)
    cfg_invalid.max_token_limit = 195_000
    with pytest.raises(ValueError, match="cannot exceed the planner model's context window limit"):
        validate_checkpoint_config_for_model_info(cfg_invalid, 200_000, 8_192)


def test_parse_compact_request() -> None:
    """Verify parse_compact_request for /compact and /offload commands."""
    assert parse_compact_request("/compact") == (CompactRequestType.COMPACT_NO_ARGS, "")
    assert parse_compact_request("  /offload  ") == (CompactRequestType.COMPACT_NO_ARGS, "")
    assert parse_compact_request("/compact focus on auth module") == (
        CompactRequestType.COMPACT_WITH_INSTRUCTIONS,
        "focus on auth module",
    )
    assert parse_compact_request("hello world") == (CompactRequestType.NONE, "")


def test_water_fill_allocation_and_middle_truncate() -> None:
    """Verify water-fill byte budget allocation and UTF-8-safe middle truncation."""
    # Short items (20, 30) keep exact lengths; remaining 250 bytes split evenly (125, 125)
    lengths = [20, 30, 500, 500]
    alloc = get_water_fill_allocation(lengths, 300)
    assert alloc == [20, 30, 125, 125]
    assert sum(alloc) == 300

    long_str = "A" * 500 + "B" * 500 + "C" * 500
    truncated, was_trunc, omitted, total = middle_truncate_bytes(long_str, 200)
    assert was_trunc is True
    assert total == 1500
    assert omitted == 1300
    assert "<truncated 1300 of 1500 bytes from the middle of the output>" in truncated
    assert truncated.startswith("A" * 100)
    assert truncated.endswith("C" * 100)


def test_dual_jsonl_transcript_logging_preserves_full_history(tmp_path: Path) -> None:
    """Verify transcript.jsonl (with truncated_fields) and transcript_full.jsonl (lossless 1-indexed)."""
    big_content = "USER_PAYLOAD_" + ("X" * 2000)
    big_thinking = "THINKING_CHAIN_" + ("Y" * 2000)
    big_tool_arg = "Z" * 600

    messages = [
        HumanMessage(content=big_content),
        AIMessage(
            content="Calling tool with large payload",
            additional_kwargs={"thinking": big_thinking},
            tool_calls=[
                {
                    "id": "call_1",
                    "name": "write_to_file",
                    "args": {"TargetFile": "/repo/app.py", "CodeContent": big_tool_arg},
                }
            ],
        ),
        ToolMessage(content="File written", tool_call_id="call_1", name="write_to_file"),
    ]

    compact_path, full_path = sync_messages_to_transcript(
        messages,
        thread_id="thread-lossless-test",
        data_dir=tmp_path,
    )

    assert compact_path.exists()
    assert full_path.exists()

    compact_lines = compact_path.read_text(encoding="utf-8").splitlines()
    full_lines = full_path.read_text(encoding="utf-8").splitlines()
    assert len(compact_lines) == 3
    assert len(full_lines) == 3

    # Line 1: USER_INPUT with truncated content in compact, full content in transcript_full.jsonl
    c1 = json.loads(compact_lines[0])
    f1 = json.loads(full_lines[0])
    assert c1["step_index"] == 1
    assert f1["step_index"] == 1
    assert c1["type"] == "USER_INPUT"
    assert "content" in c1["truncated_fields"]
    assert "truncated_fields" not in f1
    assert f1["content"] == big_content
    assert len(c1["content"]) < len(big_content)

    # Line 2: PLANNER_RESPONSE with truncated thinking and tool_calls in compact, full in transcript_full.jsonl
    c2 = json.loads(compact_lines[1])
    f2 = json.loads(full_lines[1])
    assert c2["step_index"] == 2
    assert f2["step_index"] == 2
    assert set(c2["truncated_fields"]) == {"thinking", "tool_calls"}
    assert "truncated_fields" not in f2
    assert f2["thinking"] == big_thinking
    assert big_tool_arg in f2["tool_calls"][0]["arguments_json"]

    # Idempotency check: calling sync_messages_to_transcript again with same messages does not duplicate lines
    sync_messages_to_transcript(messages, thread_id="thread-lossless-test", data_dir=tmp_path)
    assert len(compact_path.read_text(encoding="utf-8").splitlines()) == 3


def test_tool_output_disk_offloading(tmp_path: Path) -> None:
    """Verify tool output offloading writes full output to .system_generated/steps/<step>/output.txt."""
    huge_output = "HEADER_LINE\n" + ("0123456789ABCDEF\n" * 3000) + "FOOTER_LINE"
    tm = ToolMessage(content=huge_output, tool_call_id="tc_99", name="run_command")

    bounded = truncate_and_offload_tool_output(
        tm,
        thread_id="thread-step-offload",
        step_index=7,
        max_bytes=1024,
        data_dir=tmp_path,
    )

    expected_file = (
        tmp_path / "brain" / "thread-step-offload" / ".system_generated" / "steps" / "7" / "output.txt"
    )
    assert expected_file.exists()
    assert expected_file.read_text(encoding="utf-8") == huge_output
    assert "The tool's output was truncated due to being too long." in str(bounded.content)
    assert str(expected_file.resolve()) in str(bounded.content)
    assert "HEADER_LINE" in str(bounded.content)
    assert "FOOTER_LINE" in str(bounded.content)


def test_window_slicing_tool_call_pairing_and_turn_invariants() -> None:
    """Verify unpaired tool-call/result pruning and non-tool-call window start invariants."""
    messages = [
        # Orphaned ToolMessage at start of sliced window (its AIMessage was before cutoff)
        ToolMessage(content="orphan result", tool_call_id="orphan_tc_1", name="read_file"),
        # AIMessage with one paired tool call and one unpaired tool call
        AIMessage(
            content="",
            tool_calls=[
                {"id": "paired_tc_2", "name": "view_file", "args": {"AbsolutePath": "/a.py"}},
                {"id": "unpaired_tc_3", "name": "view_file", "args": {"AbsolutePath": "/b.py"}},
            ],
        ),
        ToolMessage(content="file a content", tool_call_id="paired_tc_2", name="view_file"),
    ]

    paired = remove_unpaired_tool_calls_and_results(messages)
    assert len(paired) == 2
    assert isinstance(paired[0], AIMessage)
    assert [tc["id"] for tc in paired[0].tool_calls] == ["paired_tc_2"]
    assert isinstance(paired[1], ToolMessage)
    assert paired[1].tool_call_id == "paired_tc_2"

    # Since paired[0] is an AIMessage with tool_calls at the start of the window,
    # ensure_not_starting_with_tool_call must prepend a synthetic "Continue" HumanMessage
    valid_turns = ensure_not_starting_with_tool_call(paired)
    assert len(valid_turns) == 3
    assert isinstance(valid_turns[0], HumanMessage)
    assert valid_turns[0].content == "Continue"


def test_llm_continuation_summary_retries_on_tool_call_and_wraps_envelope(tmp_path: Path) -> None:
    """Verify tool-call rejection/retry during summarization and <CONTEXT_SUMMARY> envelope formatting."""
    # First model response illegally tries to call a tool; second response returns valid <summary>
    bad_ai = AIMessage(
        content="",
        tool_calls=[{"id": "tc_bad", "name": "read_file", "args": {"path": "foo.py"}}],
    )
    good_ai = AIMessage(
        content=(
            "<summary>\n"
            "## 1. Task Overview\n- Build workspace trust and compaction.\n"
            "## 2. Progress\n- Completed architecture analysis.\n"
            "## 3. Key Findings\n- Dual transcripts prevent data loss.\n"
            "## 4. Active Context\n- Editing compaction.py.\n"
            "## 5. Next Steps\n- Run pytest.\n"
            "## 6. Commitments & Constraints\n- Preserve full history.\n"
            "</summary>"
        )
    )

    mock_model = MagicMock()
    mock_model.invoke = MagicMock(side_effect=[bad_ai, good_ai])

    history = [
        HumanMessage(content="First user request: analyze workspace trust"),
        AIMessage(
            content="Reading cli_settings.py",
            tool_calls=[
                {
                    "id": "tc_1",
                    "name": "write_to_file",
                    "args": {"TargetFile": "/repo/src/opscloud/config/cli_settings.py"},
                },
                {
                    "id": "tc_sub",
                    "name": "invoke_subagent",
                    "args": {"name": "research", "prompt": "Scan symbols"},
                },
            ],
        ),
        ToolMessage(content="Done", tool_call_id="tc_1", name="write_to_file"),
        HumanMessage(content="Second user request: now analyze compaction"),
    ]

    # Create an artifact file in the thread's brain dir
    art_dir = tmp_path / "brain" / "thread-env-test"
    art_dir.mkdir(parents=True, exist_ok=True)
    (art_dir / "analysis_report.md").write_text("# Report", encoding="utf-8")

    summary = generate_llm_continuation_summary(
        mock_model,
        history,
        custom_instructions="Preserve all file paths",
    )
    assert mock_model.invoke.call_count == 2
    assert "<summary>" in summary
    assert "## 1. Task Overview" in summary

    envelope = build_context_summary_envelope(
        history,
        summary,
        thread_id="thread-env-test",
        data_dir=tmp_path,
    )
    assert envelope.startswith("<CONTEXT_SUMMARY>")
    assert envelope.endswith("</CONTEXT_SUMMARY>")
    assert "# USER Objective:\nFirst user request: analyze workspace trust" in envelope
    assert "1. First user request: analyze workspace trust" in envelope
    assert "2. Second user request: now analyze compaction" in envelope
    assert "# Previous Session Summary:" in envelope
    assert "# Code Interaction Summary:" in envelope
    assert "`/repo/src/opscloud/config/cli_settings.py` (edited)" in envelope
    assert "[ARTIFACT: analysis_report]" in envelope
    assert "# Subagents (1 total)" in envelope
    assert "# Conversation Logs" in envelope
    assert "transcript.jsonl" in envelope
    assert "transcript_full.jsonl" in envelope

    # Verify carry-forward across a second compaction!
    second_turn_history = [
        SystemMessage(content=envelope),
        HumanMessage(content="Third user request: run all unit tests"),
        AIMessage(
            content="Viewing test file",
            tool_calls=[
                {
                    "id": "tc_2",
                    "name": "view_file",
                    "args": {"AbsolutePath": "/repo/tests/test_compaction.py"},
                }
            ],
        ),
        ToolMessage(content="ok", tool_call_id="tc_2", name="view_file"),
    ]
    obj2, reqs2 = capture_user_requests(second_turn_history)
    assert obj2 == "First user request: analyze workspace trust"
    assert reqs2 == [
        "First user request: analyze workspace trust",
        "Second user request: now analyze compaction",
        "Third user request: run all unit tests",
    ]
    files2 = compute_trajectory_file_interactions(second_turn_history)
    assert "- `/repo/src/opscloud/config/cli_settings.py` (edited)" in files2
    assert "- `/repo/tests/test_compaction.py` (viewed)" in files2


@pytest.mark.asyncio
async def test_compact_command_handler_lossless_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify /compact command writes dual transcripts and builds <CONTEXT_SUMMARY> without losing data."""
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)

    messages = [
        HumanMessage(id="m1", content="Initial goal: implement lossless compaction in opscloud"),
        AIMessage(
            id="m2",
            content="Inspecting middleware",
            tool_calls=[
                {
                    "id": "call_view",
                    "name": "view_file",
                    "args": {"AbsolutePath": "/Users/structbinary/Documents/work/talkops/opscloud/src/opscloud/middleware/compaction.py"},
                }
            ],
        ),
        ToolMessage(
            id="m3",
            content="File contents: " + ("A" * 1500),
            tool_call_id="call_view",
            name="view_file",
        ),
        HumanMessage(id="m4", content="Make sure older messages are never lost"),
        AIMessage(id="m5", content="I will add dual JSONL transcripts and <CONTEXT_SUMMARY>."),
    ]

    mock_app = MagicMock()
    mock_app._agent_running = False
    mock_app._agent_thread_id = "test-compact-cmd-thread"
    mock_app.get_thread_messages = MagicMock(return_value=messages)
    mock_app._active_model = None

    mock_agent = MagicMock()
    mock_agent.aupdate_state = AsyncMock()
    mock_state = MagicMock()
    mock_state.values = {"messages": messages[-2:]}
    mock_agent.aget_state = AsyncMock(return_value=mock_state)

    handler = CompactHandler()
    ctx = CommandContext(
        app=mock_app,
        agent=mock_agent,
        raw_command="/compact keep all file paths and test details",
        args="keep all file paths and test details",
        thread_id="test-compact-cmd-thread",
    )

    result = await handler.execute(ctx)
    assert result.success is True
    assert "Conversation Compacted" in (result.message or "")
    assert "transcript.jsonl" in (result.message or "")

    # Verify both transcripts were written with all 5 messages before compaction
    compact_path = Path(result.data["transcript_path"])
    full_path = Path(result.data["full_transcript_path"])
    assert compact_path.exists()
    assert full_path.exists()
    assert len(compact_path.read_text(encoding="utf-8").splitlines()) == 5
    assert len(full_path.read_text(encoding="utf-8").splitlines()) == 5

    # Verify the <CONTEXT_SUMMARY> envelope contains the initial user objective,
    # chronological user requests, file interactions, and <COMPACTED_HISTORY>
    ctx_summary = result.data["context_summary"]
    assert "<CONTEXT_SUMMARY>" in ctx_summary
    assert "Initial goal: implement lossless compaction in opscloud" in ctx_summary
    assert "keep all file paths and test details" in ctx_summary
    assert "# Code Interaction Summary:" in ctx_summary
    assert "<COMPACTED_HISTORY>" in ctx_summary


def test_cli_compaction_middleware_injects_transcript_section() -> None:
    """Verify _create_cli_compaction_middleware injects <conversation_transcript> into system_prompt."""
    fake_model = FakeListChatModel(responses=["ok"])
    mw = _create_cli_compaction_middleware(fake_model, _DummyBackend())
    assert mw.system_prompt is not None
    assert "<conversation_transcript>" in mw.system_prompt
    assert CONVERSATION_TRANSCRIPT_SYSTEM_SECTION in mw.system_prompt


def test_opscloud_summarization_middleware_and_compact_tool_in_sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify automatic middleware summarization and compact_conversation tool emit <CONTEXT_SUMMARY>."""
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)

    fake_model = FakeListChatModel(
        responses=[
            "<summary>\n"
            "## 1. Task Overview\n- Unified middleware + tool compaction.\n"
            "## 2. Progress\n- Synced all paths.\n"
            "## 3. Key Findings\n- All three compaction triggers share the same engine.\n"
            "## 4. Active Context\n- Testing CLICompactionMiddleware.\n"
            "## 5. Next Steps\n- Complete verification.\n"
            "## 6. Commitments & Constraints\n- Preserve full history.\n"
            "</summary>"
        ]
    )
    mw = _create_cli_compaction_middleware(fake_model, _DummyBackend())
    assert isinstance(mw._summarization, OpsCloudSummarizationMiddleware)

    mw._summarization._active_thread_id = "sync-mw-thread"
    messages = [
        HumanMessage(id="m1", content="Check how middleware and tool compaction stay in sync"),
        AIMessage(
            id="m2",
            content="Inspecting compaction.py",
            tool_calls=[
                {
                    "id": "call_1",
                    "name": "read_file",
                    "args": {"file_path": "/Users/structbinary/Documents/work/talkops/opscloud/src/opscloud/middleware/compaction.py"},
                }
            ],
        ),
        ToolMessage(id="m3", tool_call_id="call_1", name="read_file", content="class CLICompactionMiddleware..."),
    ]

    summary_envelope = mw._summarization._create_summary(messages)
    built_msgs = mw._summarization._build_new_messages_with_path(
        summary_envelope,
        "/conversation_history/session_123.md",
    )
    assert len(built_msgs) == 1
    msg_content = str(built_msgs[0].content)
    assert msg_content.startswith("<CONTEXT_SUMMARY>")
    assert msg_content.endswith("</CONTEXT_SUMMARY>")
    assert "# USER Objective:\nCheck how middleware and tool compaction stay in sync" in msg_content
    assert "## 1. Task Overview\n- Unified middleware + tool compaction." in msg_content
    assert "# Code Interaction Summary:" in msg_content
    assert "transcript.jsonl" in msg_content
    assert "transcript_full.jsonl" in msg_content
    assert "/conversation_history/session_123.md" in msg_content


def test_deduplicate_unchanged_file_reads() -> None:
    """Verify earlier identical read_file outputs are deduplicated while the latest read stays intact."""
    from opscloud.middleware.compaction import deduplicate_unchanged_file_reads

    file_body = "def handler():\n    return 200\n" * 50
    messages = [
        HumanMessage(content="Inspect app.py twice"),
        AIMessage(
            content="",
            tool_calls=[
                {"id": "read_1", "name": "read_file", "args": {"file_path": "/repo/app.py", "offset": 0, "limit": 100}},
            ],
        ),
        ToolMessage(content=file_body, tool_call_id="read_1", name="read_file"),
        AIMessage(
            content="",
            tool_calls=[
                {"id": "read_2", "name": "read_file", "args": {"file_path": "/repo/app.py", "offset": 0, "limit": 100}},
            ],
        ),
        ToolMessage(content=file_body, tool_call_id="read_2", name="read_file"),
    ]

    deduped = deduplicate_unchanged_file_reads(messages)
    assert "[file_unchanged: duplicate read_file output for '/repo/app.py'" in str(deduped[2].content)
    assert deduped[4].content == file_body


def test_microcompact_tool_messages_keeps_recent_and_clears_older() -> None:
    """Verify Tier-1 keep-recent microcompaction clears older heavy tool outputs while keeping recent ones."""
    from opscloud.middleware.compaction import microcompact_tool_messages

    heavy_output = "LINE_DATA_" + ("0123456789" * 600)  # ~6,010 chars each
    messages: list[Any] = [HumanMessage(content="Run multiple commands")]
    for idx in range(10):
        tc_id = f"tc_{idx}"
        messages.append(
            AIMessage(
                content="",
                tool_calls=[{"id": tc_id, "name": "execute", "args": {"command": f"cmd {idx}"}}],
            )
        )
        messages.append(ToolMessage(content=heavy_output, tool_call_id=tc_id, name="execute"))

    compacted = microcompact_tool_messages(
        messages,
        keep_recent_tool_uses=3,
        min_saved_chars=10_000,
        min_message_chars=1_000,
    )
    tool_msgs = [m for m in compacted if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 10
    # First 7 older tool messages are microcompacted
    for old_tm in tool_msgs[:7]:
        assert "[Old tool result content cleared by microcompaction" in str(old_tm.content)
        assert "from execute)" in str(old_tm.content)
    # Most recent 3 tool messages remain intact
    for recent_tm in tool_msgs[7:]:
        assert recent_tm.content == heavy_output



