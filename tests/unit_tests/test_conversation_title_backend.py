"""Unit and integration tests for Phase 1 backend conversation title generation and SQLite persistence."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage
import pytest

from opscloud.middleware.resume_state import ResumeStateMiddleware
from opscloud.state.conversation_title import (
    CONVERSATION_TITLE_SYSTEM_PROMPT,
    MAX_SUMMARY_PREVIEW_CHARS,
    clean_prompt_for_title,
    generate_conversation_title,
    get_thread_display_label,
    maybe_generate_conversation_title,
    postprocess_conversation_title,
    schedule_conversation_title_generation,
    truncate_summary_preview,
)
from opscloud.state.session import (
    SessionManager,
    delete_thread,
    fork_thread,
    get_checkpointer,
    get_thread_title,
    list_threads,
    set_db_path,
    set_thread_title,
)


def test_exact_agy_system_prompt_byte_length() -> None:
    """Verify the system prompt matches the exact 522-byte (0x20a) string from agy @ 0x10331d50e."""
    assert len(CONVERSATION_TITLE_SYSTEM_PROMPT.encode("utf-8")) == 522
    assert "Respond with ONLY a short conversation title (3-5 words, title-cased, no prefix)" in CONVERSATION_TITLE_SYSTEM_PROMPT


def test_truncate_summary_preview_and_postprocess_title() -> None:
    """Verify TruncateSummaryPreview and postprocessConversationTitle match agy semantics."""
    assert truncate_summary_preview(None) == ""
    assert truncate_summary_preview("   \n  ") == ""
    assert truncate_summary_preview("  First line summary \nSecond line ignored  ") == "First line summary"

    long_str = "A" * 600
    assert len(truncate_summary_preview(long_str)) == MAX_SUMMARY_PREVIEW_CHARS

    # postprocessConversationTitle strips "\"'`#* " and keeps only the first line
    assert postprocess_conversation_title('  "Refining SageMaker Agent Plugin"\nExtra explanation ') == (
        "Refining SageMaker Agent Plugin"
    )
    assert postprocess_conversation_title("### **Debugging ECS Service Mesh**`") == (
        "Debugging ECS Service Mesh"
    )
    assert postprocess_conversation_title("  'Investigating   RDS    Latency'  ") == (
        "Investigating RDS Latency"
    )
    assert postprocess_conversation_title("\"'`#* ") == ""


def test_clean_prompt_for_title_strips_command_tags() -> None:
    """Verify XML command wrappers are cleaned before title generation and fallback preview."""
    raw = (
        "<command-message>deep-check</command-message>\n"
        "<command-name>/deep-check</command-name>\n"
        "<command-args>production-vpc</command-args>"
    )
    cleaned = clean_prompt_for_title(raw)
    assert "<command-message>" not in cleaned
    assert "/deep-check" in cleaned
    assert "production-vpc" in cleaned


def test_get_thread_display_label_fallback_order() -> None:
    """Verify Title -> Initial Prompt -> Fallback/Thread ID priority matching getDisplayLabel (0x1027b8d80)."""
    # 1. Uses title when present
    t1 = {
        "thread_id": "thread-1",
        "title": ' "Refining SageMaker Agent Plugin" ',
        "initial_prompt": "can you refine the sagemaker agent plugin",
    }
    assert get_thread_display_label(t1) == "Refining SageMaker Agent Plugin"

    # 2. Falls back to cleaned first line of initial_prompt when title is absent/empty
    t2 = {
        "thread_id": "thread-2",
        "title": None,
        "initial_prompt": "  what is yours capability ? \n second line ",
    }
    assert get_thread_display_label(t2) == "what is yours capability ?"

    # 3. Cleans XML command tags on fallback preview
    t3 = {
        "thread_id": "thread-3",
        "initial_prompt": "<command-message>deep-check</command-message><command-name>/deep-check</command-name>",
    }
    assert get_thread_display_label(t3) == "/deep-check"

    # 4. Falls back to default fallback or thread_id when both are empty
    t4 = {"thread_id": "thread-4", "title": "", "initial_prompt": ""}
    assert get_thread_display_label(t4) == "(No initial prompt)"
    assert get_thread_display_label(t4, fallback="") == "thread-4"


@pytest.mark.asyncio
async def test_generate_conversation_title_retries_and_isolates_callbacks() -> None:
    """Verify 3-attempt non-streaming retry loop and callback isolation."""
    calls: list[dict[str, Any]] = []

    class FlakyTitleModel(FakeListChatModel):
        async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> AIMessage:
            calls.append({"input": input, "config": config})
            if len(calls) < 2:
                raise RuntimeError("Transient rate limit")
            return AIMessage(content=' "**Auditing AWS IAM Roles**"\nHere is your title.')

    model = FlakyTitleModel(responses=["unused"])
    title = await generate_conversation_title(
        "Please audit all AWS IAM roles in our account for wildcard permissions",
        model=model,
    )
    assert title == "Auditing AWS IAM Roles"
    assert len(calls) == 2
    assert calls[0]["config"]["callbacks"] == []
    assert calls[0]["config"]["metadata"]["lc_source"] == "conversation_title_generator"


@pytest.mark.asyncio
async def test_sqlite_thread_summaries_persistence_and_list_threads(tmp_path: Path) -> None:
    """Verify SQLite thread_summaries persistence, only_if_empty guard, list_threads, fork, and delete."""
    db_file = tmp_path / "test_sessions.db"
    set_db_path(db_file)

    thread_id = "019d8c18-1111-7000-8000-000000000001"

    # Create a real checkpoint for thread_id via OpsCloudCheckpointer
    async with get_checkpointer() as saver:
        await saver.setup()
        config: Any = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": "",
            }
        }
        checkpoint: Any = {
            "v": 1,
            "id": "1ef00000-0000-6000-8000-000000000001",
            "ts": "2026-10-15T12:00:00+00:00",
            "channel_values": {
                "messages": [HumanMessage(content="how do we optimize eks node groups?")],
            },
            "channel_versions": {"messages": 1},
            "versions_seen": {},
            "pending_sends": [],
        }
        metadata: Any = {
            "source": "loop",
            "step": 0,
            "writes": {},
            "updated_at": "2026-10-15T12:00:00+00:00",
            "agent_name": "opscloud",
        }
        await saver.aput(config, checkpoint, metadata, {"messages": 1})

    # Initially no title is set
    assert await get_thread_title(thread_id) is None
    threads_before = await list_threads(limit=10)
    assert len(threads_before) == 1
    assert threads_before[0]["thread_id"] == thread_id
    assert threads_before[0].get("title") is None
    assert threads_before[0].get("initial_prompt") == "how do we optimize eks node groups?"

    # Generate and persist title via maybe_generate_conversation_title
    fake_model = FakeListChatModel(responses=['"Optimizing EKS Node Groups"'])
    generated = await maybe_generate_conversation_title(
        thread_id,
        "how do we optimize eks node groups?",
        model=fake_model,
    )
    assert generated == "Optimizing EKS Node Groups"
    assert await get_thread_title(thread_id) == "Optimizing EKS Node Groups"

    # Subsequent call with only_if_empty=True must not overwrite existing title
    overwritten = await set_thread_title(
        thread_id,
        "Different Title",
        only_if_empty=True,
    )
    assert overwritten is False
    assert await get_thread_title(thread_id) == "Optimizing EKS Node Groups"

    # Explicit overwrite with only_if_empty=False updates title
    updated = await set_thread_title(
        thread_id,
        "Scaling EKS Node Groups",
        only_if_empty=False,
    )
    assert updated is True
    assert await get_thread_title(thread_id) == "Scaling EKS Node Groups"

    # list_threads() now includes the persisted title
    threads_after = await SessionManager.list_threads(limit=10)
    assert len(threads_after) == 1
    assert threads_after[0].get("title") == "Scaling EKS Node Groups"
    assert get_thread_display_label(threads_after[0]) == "Scaling EKS Node Groups"

    # Forking the thread copies the title to the new thread
    forked_id = await fork_thread(thread_id, target_thread_id="019d8c18-2222-7000-8000-000000000002")
    assert await get_thread_title(forked_id) == "Scaling EKS Node Groups"

    # Deleting the thread removes checkpoints and thread_summaries
    assert await delete_thread(thread_id) is True
    assert await get_thread_title(thread_id) is None


@pytest.mark.asyncio
async def test_resume_state_middleware_schedules_title_only_on_first_root_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify ResumeStateMiddleware triggers title generation only on first turn of root thread."""
    db_file = tmp_path / "test_mw_sessions.db"
    set_db_path(db_file)

    scheduled_calls: list[tuple[str, str, str | None]] = []

    def fake_schedule(
        thread_id: str,
        prompt: str,
        *,
        model: Any = None,
        model_spec: str | None = None,
        provider: str | None = None,
    ) -> None:
        scheduled_calls.append((thread_id, prompt, model_spec))
        return None

    monkeypatch.setattr(
        "opscloud.state.conversation_title.schedule_conversation_title_generation",
        fake_schedule,
    )

    mw = ResumeStateMiddleware()

    # 1. Subagent namespace (checkpoint_ns != "") -> must skip
    subagent_runtime = SimpleNamespace(
        execution_info=SimpleNamespace(thread_id="thread-root-1", checkpoint_ns="subagent:worker"),
        context={"model": "google_genai:gemini-2.5-flash"},
        config={},
    )
    await mw.abefore_agent(
        {"messages": [HumanMessage(content="subagent task")]},
        subagent_runtime,  # type: ignore[arg-type]
    )
    assert scheduled_calls == []

    # 2. Subsequent turn (already has an AIMessage) -> must skip
    root_runtime = SimpleNamespace(
        execution_info=SimpleNamespace(thread_id="thread-root-1", checkpoint_ns=""),
        context={"model": "google_genai:gemini-2.5-flash"},
        config={},
    )
    await mw.abefore_agent(
        {
            "messages": [
                HumanMessage(content="first question"),
                AIMessage(content="first answer"),
                HumanMessage(content="second question"),
            ]
        },
        root_runtime,  # type: ignore[arg-type]
    )
    assert scheduled_calls == []

    # 3. First turn on root conversation -> schedules title generation!
    await mw.abefore_agent(
        {"messages": [HumanMessage(content="check s3 bucket encryption across regions")]},
        root_runtime,  # type: ignore[arg-type]
    )
    assert scheduled_calls == [
        (
            "thread-root-1",
            "check s3 bucket encryption across regions",
            "google_genai:gemini-2.5-flash",
        )
    ]

    # 4. Verify background task execution end-to-end with schedule_conversation_title_generation
    loop = asyncio.get_running_loop()
    loop_exceptions: list[dict[str, object]] = []
    prev_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, ctx: loop_exceptions.append(ctx))
    try:
        fake_model = FakeListChatModel(responses=['"Auditing S3 Bucket Encryption"'])
        task = schedule_conversation_title_generation(
            "thread-async-1",
            "check s3 bucket encryption across regions",
            model=fake_model,
        )
        assert task is not None
        result_title = await asyncio.wait_for(task, timeout=5.0)
        await asyncio.sleep(0)  # allow done callbacks to flush
        assert result_title == "Auditing S3 Bucket Encryption"
        assert await get_thread_title("thread-async-1") == "Auditing S3 Bucket Encryption"
        assert loop_exceptions == []
    finally:
        loop.set_exception_handler(prev_handler)
