"""CLI conversation compaction middleware and lossless transcript architecture.

Provides OpsCloud's context compaction and lossless history preservation system:
- Checkpoint configuration & token budget calculation:
  ``get_default_checkpoint_config_for_model``,
  ``get_checkpoint_config_for_model_info``,
  ``clamp_checkpoint_config_for_model_info``, and
  ``validate_checkpoint_config_for_model_info``.
- Water-fill byte budget allocation (``get_water_fill_allocation``)
  and middle-truncation (``middle_truncate_bytes``).
- Dual line-aligned JSONL transcripts (``sync_messages_to_transcript``,
  ``step_to_summary_json``, ``build_tool_call_summary``):
  writes ``.system_generated/logs/transcript.jsonl`` (compact with
  ``truncated_fields``) and ``.system_generated/logs/transcript_full.jsonl``
  (lossless 1-indexed line-aligned).
- Tool output disk offloading (``truncate_and_offload_tool_output``):
  saves oversized tool outputs to ``.system_generated/steps/<step_index>/output.txt``
  and replaces the middle of the inline output with a preview and file pointer.
- Message window slicing invariants (``remove_unpaired_tool_calls_and_results``,
  ``ensure_not_starting_with_tool_call``).
- Structured 6-section LLM continuation summary with ``ON-DEMAND COMPACTION``
  support (``generate_llm_continuation_summary``, ``agenerate_llm_continuation_summary``,
  ``generate_fallback_summary``) and ``<CONTEXT_SUMMARY>`` envelope formatting
  (``build_context_summary_envelope``).
- System prompt ``<conversation_transcript>`` section
  (``CONVERSATION_TRANSCRIPT_SYSTEM_SECTION``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from functools import partial
import json
import os
from pathlib import Path
import re
from typing import TYPE_CHECKING, Annotated, Any, NamedTuple, cast
from uuid import uuid4

from deepagents.backends.protocol import (
    FILE_NOT_FOUND,
    BackendProtocol,
    EditResult,
    FileDownloadResponse,
    WriteResult,
)
from deepagents.middleware.summarization import (
    SummarizationMiddleware,
    SummarizationToolMiddleware,
    create_summarization_middleware,
    create_summarization_tool_middleware,
)
from langchain.agents.middleware.types import (
    ModelRequest,
    ModelResponse,
)
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import InjectedToolArg, StructuredTool
from langgraph.types import Command

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel
    from langgraph.prebuilt.tool_node import ToolCallRequest

from opscloud.config import paths
from opscloud.config.cli_settings import auto_compact_override, get_cli_settings_manager
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

COMPACTION_FAILURE_PREFIX = "Compaction failed"
_OFFLOAD_SEED_ID_PREFIX = "offload-seed-"

# ==============================================================================
# Compaction Budget Constants & Prompt Templates
# ==============================================================================

DEFAULT_MIN_OUTPUT_RESERVE_TOKENS = 40_000
DEFAULT_MAX_SUMMARY_OUTPUT_TOKENS = 16_384
DEFAULT_FALLBACK_MAX_TOKEN_LIMIT = 250_000
DEFAULT_LARGE_MODEL_MAX_TOKEN_LIMIT = 1_000_000
DEFAULT_OVERHEAD_RATIO = 0.15
DEFAULT_MAX_USER_REQUESTS = 20
DEFAULT_MAX_USER_REQUEST_BYTES = 4096
DEFAULT_MAX_SUBAGENTS = 10
DEFAULT_MAX_STEP_SUMMARY_BYTES = 1024
DEFAULT_MAX_TOOL_ARG_BYTES = 256
DEFAULT_MAX_FILE_DIFF_ITEMS = 300
DEFAULT_TOOL_OUTPUT_MAX_BYTES = 32_768

# Checkpoint System Prompt
CHECKPOINT_SYSTEM_PROMPT = (
    "You are an expert AI coding assistant with extreme attention to detail. You are pair programming with a USER to solve a coding task.\n"
    "You provide clear, detailed, and accurate summaries of conversations. When asked, you focus on outlining the USER's main goals\n"
    "and listing key information and context discussed. Your response should be well-organized and reflect the essence of the dialog.\n"
    "NEVER lie or make things up. Your summaries should always be grounded in the conversation."
)

# Continuation Summary Prompt
CONTINUATION_SUMMARY_PROMPT = """You have been working on the task described above but have not yet completed
it. Write a continuation summary that will allow you (or another instance of
yourself) to resume work efficiently in a future context window where the
full conversation history will NOT be available—only this summary.

This summary is all that will be available to you going forward in the future
context window. Do not call any tools, simply just provide the summary based on
the information available in the current context window.

Your summary must be structured, concise, and actionable. Optimize for enabling immediate resumption with zero redundant work.

Include the following sections:

1. **Task Overview**
   - The user's core request and success criteria
   - Constraints, preferences, or scope boundaries they specified
   - Any ambiguities that were resolved (and how)

2. **Progress**
   - What has been completed, with concrete references (file paths,
     resource identifiers, tool outputs, URLs, etc.)
   - Key artifacts produced and their current state
   - What is in progress but incomplete, and its current state

3. **Key Findings**
   - Technical constraints, requirements, or domain details uncovered
   - Decisions made and their rationale
   - Errors encountered and their resolutions
   - Approaches that were tried and abandoned (and why—this prevents
     the successor from repeating them)

4. **Active Context**
   - State of any external resources, sessions, or environments in use
   - Relevant intermediate results, hypotheses, or working assumptions
   - Dependencies between components or steps

5. **Next Steps**
   - Specific actions needed to complete the task, in priority order
   - Known blockers or open questions that must be resolved
   - For each step, note any prerequisites or risks

6. **Commitments & Constraints**
   - Promises made to the user (e.g., "I said I would do X before Y")
   - User preferences or style requirements
   - Any boundaries the user set on approach, tools, or scope

Be concise but complete—err on the side of including anything that would
prevent duplicate work, repeated mistakes, or broken promises. Do not include
information that is obvious from the task description itself.

Wrap your response in <summary></summary> tags."""

# On-Demand Compaction Suffix
ON_DEMAND_COMPACTION_SUFFIX = (
    "\n\nON-DEMAND COMPACTION:\n"
    "The user triggered this compaction. Follow any instructions in their latest message, if present."
)

# Context Summary Wrapper
CONTEXT_SUMMARY_WRAPPER = (
    "<CONTEXT_SUMMARY>\n"
    "The following is a summary of the conversation history that has been truncated to fit within the context window:\n\n"
    "%s\n"
    "</CONTEXT_SUMMARY>"
)

# Resuming from compaction intro
RESUMING_FROM_COMPACTION_INTRO = (
    "You are continuing work on the task described above, but you have lost access to the full "
    "conversation history, and need to resume work efficiently using the progress summary below:"
)

# Code Interaction Summary template
CODE_INTERACTION_SUMMARY_TEMPLATE = (
    "# Code Interaction Summary:\n"
    "The following is an important list of files that you and the user have previously edited and viewed.\n"
    "If you believe the file contents are important, you may view the file again to regain context.\n"
    "If you need to see the exact diff made, run commands using the user's version control system, if available, to see what has changed.\n"
    "If one is not available, view the file to see its current state.\n"
    "%s"
)

# Conversation Logs footer
CONVERSATION_LOGS_FOOTER_TEMPLATE = (
    "# Conversation Logs\n"
    "Start with the compact transcript; if a step has `truncated_fields`, read only that specific line in `transcript_full.jsonl`:\n"
    "- `%s`\n"
    "- `%s`"
)

# Running Background Tasks template
RUNNING_BACKGROUND_TASKS_TEMPLATE = (
    "# Running Background Tasks\n"
    "The following background tasks were running at the time this checkpoint was created.\n"
    "Use the manage_task tool to interact with them (e.g. to kill them or check their status).\n\n"
    "%s"
)

# Subagents footer template
SUBAGENTS_FOOTER_TEMPLATE = (
    "%s\n"
    "%s\n"
    "View the conversation log to see the full conversation with the subagent and use send_message with the conversation_id to communicate with them.\n\n"
    "%s"
)

# Tool Output Truncated templates
TOOL_OUTPUT_TRUNCATED_WITH_FILE_TEMPLATE = (
    "The tool's output was truncated due to being too long. You can find the full output in a file at %s.\n\n"
    "Here is a preview with the middle of the output truncated:\n"
)

TOOL_OUTPUT_TRUNCATED_NO_FILE_TEMPLATE = (
    "The tool's output was truncated due to being too long.\n\n"
    "Here is a preview with the middle of the output truncated:\n"
)

# System Prompt Transcript Guidance Section
CONVERSATION_TRANSCRIPT_SYSTEM_SECTION = """<conversation_transcript>
Transcripts for the active conversation are stored at `<dataDir>/brain/<thread-id>/.system_generated/logs/transcript.jsonl` (and `transcript_full.jsonl`).
- Start with `transcript.jsonl` (compact). When `truncated_fields` is present on a line, read only that specific 1-indexed line in `transcript_full.jsonl`.
- Each line is a single JSON object (JSONL) with `step_index`, `source`, `type`, `status`, `created_at`, `content`, `thinking`, `tool_calls`, and optional `truncated_fields`.
- When older messages are compacted into `<CONTEXT_SUMMARY>`, no history is ever lost: use `read_file` or `grep` on `transcript.jsonl` and `transcript_full.jsonl` (or `.system_generated/steps/<step_index>/output.txt` for offloaded tool outputs) whenever you need exact details from earlier turns.
</conversation_transcript>"""


# ==============================================================================
# Checkpoint Configuration & Request Parsing
# ==============================================================================


class CompactRequestType(IntEnum):
    """Classification of a user ``/compact`` or ``/offload`` request."""

    NONE = 0
    COMPACT_NO_ARGS = 1
    COMPACT_WITH_INSTRUCTIONS = 2


def parse_compact_request(raw_input: str | Sequence[Any]) -> tuple[CompactRequestType, str]:
    """Parse whether user input is a ``/compact`` (or ``/offload``) request.

    - Returns ``(CompactRequestType.NONE, "")`` if not a compact command.
    - Returns ``(CompactRequestType.COMPACT_NO_ARGS, "")`` for bare ``/compact`` or ``/offload``.
    - Returns ``(CompactRequestType.COMPACT_WITH_INSTRUCTIONS, "<instructions>")``
      when additional instructions follow ``/compact`` or ``/offload``.
    """
    if isinstance(raw_input, str):
        text = raw_input.strip()
    elif isinstance(raw_input, Sequence):
        parts: list[str] = []
        for item in raw_input:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))
            elif hasattr(item, "content"):
                parts.append(_extract_message_text(item))
        text = " ".join(p.strip() for p in parts if p and str(p).strip()).strip()
    else:
        return CompactRequestType.NONE, ""

    if not text:
        return CompactRequestType.NONE, ""

    for prefix in ("/compact", "/offload", "compact"):
        if text.lower() == prefix:
            return CompactRequestType.COMPACT_NO_ARGS, ""
        if text.lower().startswith(prefix) and len(text) > len(prefix) and text[len(prefix)].isspace():
            instructions = text[len(prefix) :].strip()
            if instructions:
                return CompactRequestType.COMPACT_WITH_INSTRUCTIONS, instructions
            return CompactRequestType.COMPACT_NO_ARGS, ""

    return CompactRequestType.NONE, ""


@dataclass
class CheckpointRetryConfig:
    """Retry configuration for LLM checkpoint summary generation."""

    max_attempts: int = 4
    initial_backoff_ms: int = 1000
    multiplier: float = 2.0
    jitter: float = 0.4


@dataclass
class CheckpointConfig:
    """Configuration for OpsCloud conversation checkpointing and context compaction."""

    enabled: bool = True
    max_token_limit: int = DEFAULT_FALLBACK_MAX_TOKEN_LIMIT
    max_output_tokens: int = DEFAULT_MAX_SUMMARY_OUTPUT_TOKENS
    overhead_ratio: float = DEFAULT_OVERHEAD_RATIO
    max_user_requests: int = DEFAULT_MAX_USER_REQUESTS
    max_user_request_bytes: int = DEFAULT_MAX_USER_REQUEST_BYTES
    max_subagents: int = DEFAULT_MAX_SUBAGENTS
    max_step_summary_bytes: int = DEFAULT_MAX_STEP_SUMMARY_BYTES
    max_tool_arg_bytes: int = DEFAULT_MAX_TOOL_ARG_BYTES
    max_file_diff_items: int = DEFAULT_MAX_FILE_DIFF_ITEMS
    tool_output_max_bytes: int = DEFAULT_TOOL_OUTPUT_MAX_BYTES
    fail_on_invalid_output: bool = True
    retry_config: CheckpointRetryConfig = field(default_factory=CheckpointRetryConfig)


def get_default_checkpoint_config_for_model(
    max_input_tokens: int,
    max_output_tokens: int = DEFAULT_MAX_SUMMARY_OUTPUT_TOKENS,
) -> CheckpointConfig:
    """Compute default checkpoint config for a model's token limits.

    - ``reserve = max(max_output_tokens, 40_000)``
    - ``max_token_limit = max(0, max_input_tokens - reserve)`` (or ``250_000`` if ``max_input_tokens <= 0``)
    - ``summary_max_output = min(max_output_tokens, 16_384)`` if ``max_output_tokens > 0`` else ``16_384``.
    """
    reserve = max(max_output_tokens, DEFAULT_MIN_OUTPUT_RESERVE_TOKENS)
    if max_input_tokens <= 0:
        max_token_limit = DEFAULT_FALLBACK_MAX_TOKEN_LIMIT
    elif reserve >= max_input_tokens:
        max_token_limit = 0
    else:
        max_token_limit = max_input_tokens - reserve

    if max_output_tokens > 0:
        summary_max_output = min(max_output_tokens, DEFAULT_MAX_SUMMARY_OUTPUT_TOKENS)
    else:
        summary_max_output = DEFAULT_MAX_SUMMARY_OUTPUT_TOKENS

    return CheckpointConfig(
        enabled=True,
        max_token_limit=max_token_limit,
        max_output_tokens=summary_max_output,
        overhead_ratio=DEFAULT_OVERHEAD_RATIO,
        max_user_requests=DEFAULT_MAX_USER_REQUESTS,
        max_user_request_bytes=DEFAULT_MAX_USER_REQUEST_BYTES,
        max_subagents=DEFAULT_MAX_SUBAGENTS,
        max_step_summary_bytes=DEFAULT_MAX_STEP_SUMMARY_BYTES,
        max_tool_arg_bytes=DEFAULT_MAX_TOOL_ARG_BYTES,
        max_file_diff_items=DEFAULT_MAX_FILE_DIFF_ITEMS,
    )


def clamp_checkpoint_config_for_model_info(
    config: CheckpointConfig,
    max_input_tokens: int,
    max_output_tokens: int = DEFAULT_MAX_SUMMARY_OUTPUT_TOKENS,
) -> CheckpointConfig:
    """Clamp ``config`` to fit within the planner model's context window."""
    reserve = max(max_output_tokens, DEFAULT_MIN_OUTPUT_RESERVE_TOKENS)
    if max_input_tokens > 0:
        available = max(0, max_input_tokens - reserve)
        if available > 0 and (config.max_token_limit <= 0 or config.max_token_limit > available):
            config.max_token_limit = available
    if max_output_tokens > 0 and config.max_output_tokens > max_output_tokens:
        config.max_output_tokens = max_output_tokens
    return config


def validate_checkpoint_config_for_model_info(
    config: CheckpointConfig,
    max_input_tokens: int,
    max_output_tokens: int = DEFAULT_MAX_SUMMARY_OUTPUT_TOKENS,
) -> None:
    """Validate ``config`` against model token limits and overhead bounds."""
    if max_input_tokens > 0:
        reserve = max(max_output_tokens, DEFAULT_MIN_OUTPUT_RESERVE_TOKENS)
        available = max(0, max_input_tokens - reserve)
        if available > 0 and config.max_token_limit > available:
            msg = (
                f"config.MaxTokenLimit ({config.max_token_limit}) cannot exceed the planner "
                f"model's context window limit ({max_input_tokens}) minus the planner reserve ({reserve})"
            )
            raise ValueError(msg)
    if max_output_tokens > 0 and config.max_output_tokens > max_output_tokens:
        msg = (
            f"config.MaxOutputTokens {config.max_output_tokens} is greater than "
            f"checkpointModelInfo.MaxOutputTokens {max_output_tokens}"
        )
        raise ValueError(msg)
    if not (0.0 <= config.overhead_ratio <= 1.0):
        msg = f"overheadRatio {config.overhead_ratio:f} is not between 0 and 1.0"
        raise ValueError(msg)


def get_checkpoint_config_for_model_info(
    max_input_tokens: int,
    max_output_tokens: int = DEFAULT_MAX_SUMMARY_OUTPUT_TOKENS,
    auto_compact_raw: str | None = None,
) -> CheckpointConfig:
    """Build, clamp, and validate a ``CheckpointConfig`` with ``autoCompact`` override support."""
    config = get_default_checkpoint_config_for_model(max_input_tokens, max_output_tokens)
    clamp_checkpoint_config_for_model_info(config, max_input_tokens, max_output_tokens)

    effective_override = auto_compact_raw
    if effective_override is None:
        with contextlib.suppress(Exception):
            effective_override = get_cli_settings_manager().get_settings().auto_compact

    if effective_override:
        normalized, ok = auto_compact_override(effective_override)
        if ok:
            if normalized.endswith("%"):
                pct = int(normalized[:-1], 10)
                base_limit = max_input_tokens if max_input_tokens > 0 else config.max_token_limit
                override_tokens = max(1, int(base_limit * (pct / 100.0)))
                config.max_token_limit = (
                    min(config.max_token_limit, override_tokens)
                    if config.max_token_limit > 0
                    else override_tokens
                )
            else:
                override_tokens = int(normalized, 10)
                config.max_token_limit = (
                    min(config.max_token_limit, override_tokens)
                    if config.max_token_limit > 0
                    else override_tokens
                )

    validate_checkpoint_config_for_model_info(config, max_input_tokens, max_output_tokens)
    return config


# ==============================================================================
# Water-Fill Byte Allocation & Middle String Truncation
# ==============================================================================


def get_water_fill_allocation(lengths: Sequence[int], total_budget: int) -> list[int]:
    """Allocate byte budgets across items using a water-filling algorithm.

    - Items whose length is ``<=`` the fair share receive their exact length.
    - Surplus budget from short items is redistributed evenly across remaining
      longer items so no space is wasted while capping total bytes to ``total_budget``.
    """
    n = len(lengths)
    if n == 0:
        return []
    if total_budget <= 0:
        return [0] * n

    clean_lengths = [max(0, int(x)) for x in lengths]
    if sum(clean_lengths) <= total_budget:
        return list(clean_lengths)

    allocations = [0] * n
    remaining_indices = list(range(n))
    remaining_budget = total_budget

    while remaining_indices:
        share = remaining_budget // len(remaining_indices)
        if share <= 0:
            for idx in remaining_indices[:remaining_budget]:
                allocations[idx] += 1
            break

        satisfied = [idx for idx in remaining_indices if clean_lengths[idx] <= share]
        if not satisfied:
            base_share = remaining_budget // len(remaining_indices)
            rem = remaining_budget % len(remaining_indices)
            for i, idx in enumerate(remaining_indices):
                allocations[idx] = base_share + (1 if i < rem else 0)
            break

        for idx in satisfied:
            allocations[idx] = clean_lengths[idx]
            remaining_budget -= clean_lengths[idx]
        satisfied_set = set(satisfied)
        remaining_indices = [idx for idx in remaining_indices if idx not in satisfied_set]

    return allocations


def middle_truncate_bytes(
    text: str,
    max_bytes: int,
    *,
    marker_format: str = "\n<truncated {omitted} of {total} bytes from the middle of the output>\n",
) -> tuple[str, bool, int, int]:
    """Middle-truncate a string if its UTF-8 byte length exceeds ``max_bytes``.

    Returns:
        Tuple of ``(result_text, was_truncated, omitted_bytes, total_bytes)``.
    """
    if not text:
        return "", False, 0, 0
    raw = text.encode("utf-8", errors="replace")
    total_bytes = len(raw)
    if max_bytes <= 0 or total_bytes <= max_bytes:
        return text, False, 0, total_bytes

    head_budget = max_bytes // 2
    tail_budget = max_bytes - head_budget
    omitted_bytes = total_bytes - (head_budget + tail_budget)
    head_str = raw[:head_budget].decode("utf-8", errors="ignore")
    tail_str = raw[total_bytes - tail_budget :].decode("utf-8", errors="ignore")
    marker = marker_format.format(omitted=omitted_bytes, total=total_bytes)
    return f"{head_str}{marker}{tail_str}", True, omitted_bytes, total_bytes


# ==============================================================================
# Dual Line-Aligned JSONL Transcripts & Tool Output Disk Offloading
# ==============================================================================


def _sanitize_thread_id(thread_id: str | None) -> str:
    raw = (thread_id or "default").strip()
    if not raw:
        return "default"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", raw)


def get_system_generated_logs_dir(thread_id: str | None, data_dir: Path | None = None) -> Path:
    """Resolve ``<data_dir>/brain/<thread_id>/.system_generated/logs``."""
    base = data_dir if data_dir is not None else paths.DATA_DIR
    return base / "brain" / _sanitize_thread_id(thread_id) / ".system_generated" / "logs"


def get_transcript_path(thread_id: str | None, data_dir: Path | None = None) -> Path:
    """Resolve compact ``transcript.jsonl`` path."""
    return get_system_generated_logs_dir(thread_id, data_dir) / "transcript.jsonl"


def get_full_transcript_path(thread_id: str | None, data_dir: Path | None = None) -> Path:
    """Resolve lossless ``transcript_full.jsonl`` path."""
    return get_system_generated_logs_dir(thread_id, data_dir) / "transcript_full.jsonl"


def get_step_output_directory_path(
    thread_id: str | None,
    step_index: int,
    data_dir: Path | None = None,
) -> Path:
    """Resolve ``<data_dir>/brain/<thread_id>/.system_generated/steps/<step_index>``."""
    base = data_dir if data_dir is not None else paths.DATA_DIR
    return base / "brain" / _sanitize_thread_id(thread_id) / ".system_generated" / "steps" / str(max(1, step_index))


def _extract_message_text(message: Any) -> str:
    if isinstance(message, str):
        return message
    content = message.get("content", "") if isinstance(message, dict) else getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") in ("text", "output_text") and "text" in block:
                    parts.append(str(block["text"]))
                elif "text" in block and block.get("type") != "thinking":
                    parts.append(str(block["text"]))
                elif "content" in block:
                    parts.append(str(block["content"]))
        return "\n".join(p for p in parts if p)
    return str(content) if content is not None else ""


def _extract_message_thinking(message: Any) -> str:
    content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
    thinking_parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("thinking", "reasoning"):
                text = block.get("thinking") or block.get("text") or block.get("summary")
                if text:
                    thinking_parts.append(str(text))
    kwargs = (
        message.get("additional_kwargs", {})
        if isinstance(message, dict)
        else getattr(message, "additional_kwargs", {})
    )
    if isinstance(kwargs, dict):
        for key in ("thinking", "reasoning", "reasoning_content"):
            val = kwargs.get(key)
            if isinstance(val, str) and val.strip():
                thinking_parts.append(val)
    return "\n".join(thinking_parts)


def _extract_message_tool_calls(message: Any) -> list[dict[str, Any]]:
    raw_calls = (
        message.get("tool_calls")
        if isinstance(message, dict)
        else getattr(message, "tool_calls", None)
    )
    if not isinstance(raw_calls, list):
        return []
    normalized: list[dict[str, Any]] = []
    for tc in raw_calls:
        if isinstance(tc, dict):
            name = str(tc.get("name", ""))
            tc_id = str(tc.get("id", ""))
            args = tc.get("args", tc.get("arguments", {}))
        else:
            name = str(getattr(tc, "name", ""))
            tc_id = str(getattr(tc, "id", ""))
            args = getattr(tc, "args", {})
        normalized.append({"id": tc_id, "name": name, "args": args})
    return normalized


def _is_context_summary_message(message: Any) -> bool:
    """Return True only if ``message`` is a ``<CONTEXT_SUMMARY>`` checkpoint envelope."""
    if isinstance(message, dict):
        role = str(message.get("role", message.get("type", ""))).lower()
    else:
        role = getattr(message, "type", type(message).__name__).lower()
    if role in ("tool", "toolmessage"):
        return False
    content_text = _extract_message_text(message).lstrip()
    return content_text.startswith("<CONTEXT_SUMMARY>")


def _classify_message_source_and_type(message: Any) -> tuple[str, str, str]:
    """Return ``(source, step_type, status)`` for the OpsCloud transcript schema."""
    if _is_context_summary_message(message):
        return "SYSTEM", "CHECKPOINT", "DONE"

    if isinstance(message, dict):
        role = str(message.get("role", message.get("type", ""))).lower()
        status_raw = str(message.get("status", "success")).lower()
    else:
        role = getattr(message, "type", type(message).__name__).lower()
        status_raw = str(getattr(message, "status", "success")).lower()

    status = "ERROR" if status_raw == "error" else "DONE"
    if role in ("human", "user", "humanmessage"):
        return "USER_EXPLICIT", "USER_INPUT", status
    if role in ("ai", "assistant", "aimessage"):
        return "MODEL", "PLANNER_RESPONSE", status
    if role in ("tool", "toolmessage"):
        return "TOOL", "TOOL_RESPONSE", status
    return "SYSTEM", "SYSTEM_MESSAGE", status


def build_tool_call_summary(
    tool_call: dict[str, Any],
    max_arg_bytes: int = DEFAULT_MAX_TOOL_ARG_BYTES,
    *,
    truncate: bool = True,
) -> tuple[dict[str, Any], bool]:
    """Format a tool call for transcript/compaction JSON and optionally truncate args."""
    tc_id = str(tool_call.get("id", ""))
    name = str(tool_call.get("name", ""))
    args = tool_call.get("args", {})
    if isinstance(args, str):
        args_json = args
    else:
        try:
            args_json = json.dumps(args, ensure_ascii=False, sort_keys=True)
        except Exception:
            args_json = str(args)

    was_truncated = False
    if truncate and max_arg_bytes > 0:
        args_json, was_truncated, _, _ = middle_truncate_bytes(
            args_json,
            max_arg_bytes,
            marker_format="...<truncated {omitted} of {total} bytes>...",
        )

    entry: dict[str, Any] = {"name": name, "arguments_json": args_json}
    if tc_id:
        entry["id"] = tc_id
    return entry, was_truncated


def step_to_summary_json(
    message: Any,
    step_index: int,
    *,
    max_content_bytes: int = DEFAULT_MAX_STEP_SUMMARY_BYTES,
    max_tool_arg_bytes: int = DEFAULT_MAX_TOOL_ARG_BYTES,
    truncate: bool = True,
    created_at: str | None = None,
) -> str:
    """Serialize a conversation step into a single-line JSON string.

    - When ``truncate=True`` (used for ``transcript.jsonl`` and fallback step
      summaries), truncates ``content``/``thinking`` to ``max_content_bytes``
      (1024) and ``tool_calls[].arguments_json`` to ``max_tool_arg_bytes`` (256),
      recording any truncated field names in ``truncated_fields``.
    - When ``truncate=False`` (used for ``transcript_full.jsonl``), preserves all
      fields in full and never emits ``truncated_fields``.
    """
    source, step_type, status = _classify_message_source_and_type(message)
    ts = created_at or datetime.now(timezone.utc).isoformat()
    content = _extract_message_text(message)
    thinking = _extract_message_thinking(message)
    raw_tool_calls = _extract_message_tool_calls(message)

    truncated_fields: list[str] = []

    if truncate and max_content_bytes > 0 and content:
        content, c_trunc, _, _ = middle_truncate_bytes(
            content,
            max_content_bytes,
            marker_format="...<truncated {omitted} of {total} bytes>...",
        )
        if c_trunc:
            truncated_fields.append("content")

    if truncate and max_content_bytes > 0 and thinking:
        thinking, t_trunc, _, _ = middle_truncate_bytes(
            thinking,
            max_content_bytes,
            marker_format="...<truncated {omitted} of {total} bytes>...",
        )
        if t_trunc:
            truncated_fields.append("thinking")

    formatted_tool_calls: list[dict[str, Any]] = []
    any_tc_truncated = False
    for tc in raw_tool_calls:
        tc_entry, tc_trunc = build_tool_call_summary(
            tc,
            max_arg_bytes=max_tool_arg_bytes,
            truncate=truncate,
        )
        formatted_tool_calls.append(tc_entry)
        if tc_trunc:
            any_tc_truncated = True
    if any_tc_truncated:
        truncated_fields.append("tool_calls")

    record: dict[str, Any] = {
        "step_index": step_index,
        "source": source,
        "type": step_type,
        "status": status,
        "created_at": ts,
    }
    if content:
        record["content"] = content
    if thinking:
        record["thinking"] = thinking
    if formatted_tool_calls:
        record["tool_calls"] = formatted_tool_calls
    if truncate and truncated_fields:
        record["truncated_fields"] = truncated_fields

    return json.dumps(record, ensure_ascii=False)


def sync_messages_to_transcript(
    messages: Sequence[Any],
    thread_id: str | None = "default",
    *,
    data_dir: Path | None = None,
    max_content_bytes: int = DEFAULT_MAX_STEP_SUMMARY_BYTES,
    max_tool_arg_bytes: int = DEFAULT_MAX_TOOL_ARG_BYTES,
) -> tuple[Path, Path]:
    """Ensure all messages in ``messages`` are persisted to dual JSONL transcripts.

    - Writes compact entries (with ``truncated_fields``) to ``transcript.jsonl``.
    - Writes full lossless entries (without truncation) to ``transcript_full.jsonl``.
    - Both files are strictly 1-indexed line-aligned so line ``N`` in
      ``transcript.jsonl`` corresponds to line ``N`` in ``transcript_full.jsonl``.
    """
    compact_path = get_transcript_path(thread_id, data_dir)
    full_path = get_full_transcript_path(thread_id, data_dir)
    compact_path.parent.mkdir(parents=True, exist_ok=True)

    existing_count = 0
    if compact_path.exists():
        with contextlib.suppress(Exception):
            existing_lines = compact_path.read_text(encoding="utf-8").splitlines()
            existing_count = len([ln for ln in existing_lines if ln.strip()])

    if len(messages) <= existing_count:
        return compact_path, full_path

    new_compact_lines: list[str] = []
    new_full_lines: list[str] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for idx in range(existing_count, len(messages)):
        step_index = idx + 1
        msg = messages[idx]
        compact_line = step_to_summary_json(
            msg,
            step_index,
            max_content_bytes=max_content_bytes,
            max_tool_arg_bytes=max_tool_arg_bytes,
            truncate=True,
            created_at=now_iso,
        )
        full_line = step_to_summary_json(
            msg,
            step_index,
            max_content_bytes=max_content_bytes,
            max_tool_arg_bytes=max_tool_arg_bytes,
            truncate=False,
            created_at=now_iso,
        )
        new_compact_lines.append(compact_line)
        new_full_lines.append(full_line)

    with compact_path.open("a", encoding="utf-8") as cf:
        cf.write("\n".join(new_compact_lines) + "\n")
    with full_path.open("a", encoding="utf-8") as ff:
        ff.write("\n".join(new_full_lines) + "\n")

    return compact_path, full_path


def truncate_and_offload_tool_output(
    tool_message: ToolMessage,
    thread_id: str | None = "default",
    step_index: int = 1,
    *,
    max_bytes: int = DEFAULT_TOOL_OUTPUT_MAX_BYTES,
    data_dir: Path | None = None,
) -> ToolMessage:
    """Offload oversized tool output to disk and return a middle-truncated ``ToolMessage``."""
    content = _extract_message_text(tool_message)
    if not content:
        return tool_message
    raw_bytes = content.encode("utf-8", errors="replace")
    if max_bytes <= 0 or len(raw_bytes) <= max_bytes:
        return tool_message

    output_file_uri: str | None = None
    try:
        step_dir = get_step_output_directory_path(thread_id, step_index, data_dir)
        step_dir.mkdir(parents=True, exist_ok=True)
        output_file = step_dir / "output.txt"
        output_file.write_bytes(raw_bytes)
        output_file_uri = str(output_file.resolve())
    except Exception as exc:
        logger.warning("output truncation: failed to write output file: %s", exc)

    preview, _, _, _ = middle_truncate_bytes(content, max_bytes)
    if output_file_uri:
        header = TOOL_OUTPUT_TRUNCATED_WITH_FILE_TEMPLATE % output_file_uri
    else:
        header = TOOL_OUTPUT_TRUNCATED_NO_FILE_TEMPLATE

    new_content = f"{header}{preview}"
    return ToolMessage(
        content=new_content,
        name=getattr(tool_message, "name", None),
        tool_call_id=getattr(tool_message, "tool_call_id", ""),
        status=getattr(tool_message, "status", "success"),
        id=getattr(tool_message, "id", None),
        artifact=getattr(tool_message, "artifact", None),
        additional_kwargs=dict(getattr(tool_message, "additional_kwargs", {}) or {}),
    )


# ==============================================================================
# Tier-1 Microcompaction & Unchanged File-Read Deduplication
# ==============================================================================

DEFAULT_MICROCOMPACT_KEEP_RECENT_TOOLS = 5
DEFAULT_MICROCOMPACT_MIN_SAVED_CHARS = 40_000
DEFAULT_MICROCOMPACT_MIN_MESSAGE_CHARS = 2_000
DEFAULT_FILE_DEDUP_MIN_CHARS = 256

MICROCOMPACT_COMPACTABLE_TOOLS: frozenset[str] = frozenset(
    {
        "read_file",
        "read",
        "execute",
        "run_command",
        "bash",
        "grep_search",
        "grep",
        "glob",
        "web_search",
        "fetch_url",
        "edit_file",
        "write_file",
        "js_eval",
    }
)

MICROCOMPACT_CLEARED_TEMPLATE = (
    "[Old tool result content cleared by microcompaction ({chars} chars from {tool_name}) "
    "— full output is preserved in .system_generated/logs/transcript_full.jsonl]"
)

FILE_UNCHANGED_STUB_TEMPLATE = (
    "[file_unchanged: duplicate read_file output for '{file_path}' ({chars} chars) deduplicated "
    "— latest identical content is present later in context]"
)


def deduplicate_unchanged_file_reads(
    messages: Sequence[Any],
    *,
    min_chars: int = DEFAULT_FILE_DEDUP_MIN_CHARS,
) -> list[Any]:
    """Collapse earlier duplicate ``read_file`` outputs when identical content appears later.

    Tracks ``(file_path, offset, limit)`` from assistant ``tool_calls`` paired with
    ``ToolMessage`` outputs. When the same file slice is read multiple times with
    identical content, only the most recent ``ToolMessage`` retains the full body;
    earlier occurrences are replaced with a compact ``file_unchanged`` stub.
    """
    if not messages:
        return []

    read_call_meta: dict[str, tuple[str, Any, Any]] = {}
    for msg in messages:
        for tc in _extract_message_tool_calls(msg):
            tc_name = str(tc.get("name", "")).lower()
            tc_id = str(tc.get("id", ""))
            if tc_id and tc_name in {"read_file", "read"}:
                args = tc.get("args", {})
                if isinstance(args, dict):
                    f_path = str(args.get("file_path") or args.get("path") or "")
                    if f_path:
                        read_call_meta[tc_id] = (
                            f_path,
                            args.get("offset", 0),
                            args.get("limit", None),
                        )

    if not read_call_meta:
        return list(messages)

    latest_index_by_key: dict[tuple[str, Any, Any, str], int] = {}
    msg_keys: dict[int, tuple[tuple[str, Any, Any, str], str, int]] = {}

    for idx, msg in enumerate(messages):
        if not isinstance(msg, ToolMessage):
            continue
        if getattr(msg, "status", "success") == "error":
            continue
        tc_id = str(getattr(msg, "tool_call_id", ""))
        meta = read_call_meta.get(tc_id)
        if meta is None:
            continue
        text = _extract_message_text(msg)
        if len(text) < min_chars or text.startswith("[file_unchanged:"):
            continue
        f_path, offset, limit = meta
        key = (f_path, offset, limit, text)
        latest_index_by_key[key] = idx
        msg_keys[idx] = (key, f_path, len(text))

    if not msg_keys:
        return list(messages)

    result: list[Any] = list(messages)
    for idx, (key, f_path, char_len) in msg_keys.items():
        if latest_index_by_key.get(key) != idx:
            orig = result[idx]
            stub = FILE_UNCHANGED_STUB_TEMPLATE.format(file_path=f_path, chars=char_len)
            result[idx] = ToolMessage(
                content=stub,
                name=getattr(orig, "name", None),
                tool_call_id=getattr(orig, "tool_call_id", ""),
                status=getattr(orig, "status", "success"),
                id=getattr(orig, "id", None),
                artifact=getattr(orig, "artifact", None),
                additional_kwargs=dict(getattr(orig, "additional_kwargs", {}) or {}),
            )
    return result


def microcompact_tool_messages(
    messages: Sequence[Any],
    *,
    keep_recent_tool_uses: int = DEFAULT_MICROCOMPACT_KEEP_RECENT_TOOLS,
    min_saved_chars: int = DEFAULT_MICROCOMPACT_MIN_SAVED_CHARS,
    min_message_chars: int = DEFAULT_MICROCOMPACT_MIN_MESSAGE_CHARS,
) -> list[Any]:
    """Tier-1 keep-recent microcompaction for heavy tool outputs.

    Preserves the most recent ``keep_recent_tool_uses`` compactable tool outputs
    in full. When older compactable tool outputs exceed ``min_saved_chars`` in
    aggregate, replaces individual older tool outputs larger than
    ``min_message_chars`` with a compact cleared stub pointing to
    ``transcript_full.jsonl``, avoiding premature LLM conversation summarization.
    """
    if not messages:
        return []

    call_name_by_id: dict[str, str] = {}
    for msg in messages:
        for tc in _extract_message_tool_calls(msg):
            tc_id = str(tc.get("id", ""))
            tc_name = str(tc.get("name", ""))
            if tc_id and tc_name:
                call_name_by_id[tc_id] = tc_name

    compactable_indices: list[int] = []
    for idx, msg in enumerate(messages):
        if not isinstance(msg, ToolMessage):
            continue
        tc_id = str(getattr(msg, "tool_call_id", ""))
        tool_name = str(getattr(msg, "name", "") or call_name_by_id.get(tc_id, "")).lower()
        if not tool_name or tool_name in MICROCOMPACT_COMPACTABLE_TOOLS:
            compactable_indices.append(idx)

    keep_count = max(0, int(keep_recent_tool_uses))
    if len(compactable_indices) <= keep_count:
        return list(messages)

    older_indices = compactable_indices[:-keep_count] if keep_count > 0 else compactable_indices
    candidates: list[tuple[int, str, int, str]] = []
    total_saved = 0

    for idx in older_indices:
        msg = messages[idx]
        text = _extract_message_text(msg)
        if (
            len(text) < min_message_chars
            or text.startswith("[Old tool result content cleared")
            or text.startswith("[file_unchanged:")
        ):
            continue
        tc_id = str(getattr(msg, "tool_call_id", ""))
        tool_name = str(getattr(msg, "name", "") or call_name_by_id.get(tc_id, "") or "tool")
        stub = MICROCOMPACT_CLEARED_TEMPLATE.format(chars=len(text), tool_name=tool_name)
        saved = max(0, len(text) - len(stub))
        if saved > 0:
            candidates.append((idx, tool_name, len(text), stub))
            total_saved += saved

    if total_saved < min_saved_chars or not candidates:
        return list(messages)

    result: list[Any] = list(messages)
    for idx, _tool_name, _orig_len, stub in candidates:
        orig = result[idx]
        result[idx] = ToolMessage(
            content=stub,
            name=getattr(orig, "name", None),
            tool_call_id=getattr(orig, "tool_call_id", ""),
            status=getattr(orig, "status", "success"),
            id=getattr(orig, "id", None),
            artifact=getattr(orig, "artifact", None),
            additional_kwargs=dict(getattr(orig, "additional_kwargs", {}) or {}),
        )
    return result


# ==============================================================================
# Window Slicing & Tool-Call Pairing Safety
# ==============================================================================


def remove_unpaired_tool_calls_and_results(messages: Sequence[Any]) -> list[Any]:
    """Remove orphaned tool calls and orphaned tool results from a sliced message window.

    - Collects all ``tool_call_id``s present in assistant ``tool_calls`` and all
      ``tool_call_id``s present in ``ToolMessage``s within ``messages``.
    - Keeps only ``tool_call_id``s that appear in BOTH sets.
    - Strips unpaired ``tool_calls`` from assistant messages (dropping the assistant
      message only if it had no text content and all its tool calls were unpaired),
      and drops any ``ToolMessage`` whose ``tool_call_id`` is unpaired.
    """
    call_ids: set[str] = set()
    result_ids: set[str] = set()

    for msg in messages:
        for tc in _extract_message_tool_calls(msg):
            tc_id = tc.get("id", "")
            if tc_id:
                call_ids.add(tc_id)
        if isinstance(msg, ToolMessage) or (isinstance(msg, dict) and msg.get("role") == "tool"):
            tc_id = (
                msg.get("tool_call_id", "")
                if isinstance(msg, dict)
                else getattr(msg, "tool_call_id", "")
            )
            if tc_id:
                result_ids.add(str(tc_id))

    paired_ids = call_ids & result_ids
    cleaned: list[Any] = []

    for msg in messages:
        if isinstance(msg, ToolMessage) or (isinstance(msg, dict) and msg.get("role") == "tool"):
            tc_id = (
                msg.get("tool_call_id", "")
                if isinstance(msg, dict)
                else getattr(msg, "tool_call_id", "")
            )
            if tc_id and str(tc_id) in paired_ids:
                cleaned.append(msg)
            continue

        raw_tcs = (
            msg.get("tool_calls")
            if isinstance(msg, dict)
            else getattr(msg, "tool_calls", None)
        )
        if isinstance(raw_tcs, list) and raw_tcs:
            kept_tcs = [
                tc
                for tc in raw_tcs
                if (tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)) in paired_ids
            ]
            if len(kept_tcs) == len(raw_tcs):
                cleaned.append(msg)
            elif kept_tcs:
                if isinstance(msg, AIMessage):
                    cleaned.append(
                        AIMessage(
                            content=msg.content,
                            tool_calls=kept_tcs,
                            id=msg.id,
                            name=msg.name,
                            additional_kwargs=dict(msg.additional_kwargs),
                            response_metadata=dict(msg.response_metadata),
                        )
                    )
                elif isinstance(msg, dict):
                    copied = dict(msg)
                    copied["tool_calls"] = kept_tcs
                    cleaned.append(copied)
            else:
                text = _extract_message_text(msg).strip()
                if text:
                    if isinstance(msg, AIMessage):
                        cleaned.append(
                            AIMessage(
                                content=msg.content,
                                tool_calls=[],
                                id=msg.id,
                                name=msg.name,
                                additional_kwargs=dict(msg.additional_kwargs),
                                response_metadata=dict(msg.response_metadata),
                            )
                        )
                    elif isinstance(msg, dict):
                        copied = dict(msg)
                        copied.pop("tool_calls", None)
                        cleaned.append(copied)
            continue

        cleaned.append(msg)

    return cleaned


def ensure_not_starting_with_tool_call(messages: Sequence[Any]) -> list[Any]:
    """Ensure a truncated conversation window does not start with an assistant tool call.

    If the first non-system message in ``messages`` is an assistant message with
    ``tool_calls``, prepends a synthetic ``HumanMessage(content="Continue")`` before
    that assistant message so the model turn structure remains valid.
    """
    result = list(messages)
    for idx, msg in enumerate(result):
        source, step_type, _ = _classify_message_source_and_type(msg)
        if source == "SYSTEM" and step_type != "CHECKPOINT":
            continue
        tcs = _extract_message_tool_calls(msg)
        if source == "MODEL" and tcs:
            result.insert(idx, HumanMessage(content="Continue"))
        break
    return result


# ==============================================================================
# Auxiliary Checkpoint Data & <CONTEXT_SUMMARY> Generation
# ==============================================================================


def capture_user_requests(
    messages: Sequence[Any],
    *,
    max_requests: int = DEFAULT_MAX_USER_REQUESTS,
    max_total_bytes: int = DEFAULT_MAX_USER_REQUEST_BYTES,
) -> tuple[str | None, list[str]]:
    """Extract the initial user objective and chronological recent user requests.

    - Extracts any prior ``# User Requests`` and ``# USER Objective:`` embedded in
      an earlier ``<CONTEXT_SUMMARY>`` so user requests are never lost across
      repeated compactions.
    - Collects all ``USER_INPUT`` messages in chronological order (excluding bare
      ``/compact`` or ``/offload`` triggers).
    - Keeps the most recent ``max_requests`` (20) and allocates ``max_total_bytes``
      (4096) across them using ``get_water_fill_allocation``.
    """
    prior_objective: str | None = None
    all_requests: list[str] = []

    for msg in messages:
        text = _extract_message_text(msg).strip()
        if not text:
            continue
        if _is_context_summary_message(msg):
            obj_match = re.search(
                r"# USER Objective:\n(.*?)(?=\n# |\n</CONTEXT_SUMMARY>|$)",
                text,
                flags=re.DOTALL,
            )
            if obj_match and not prior_objective:
                prior_objective = obj_match.group(1).strip()

            req_match = re.search(
                r"# User Requests\nThe following were .*?in chronological order:\n(.*?)(?=\n# |\n</CONTEXT_SUMMARY>|$)",
                text,
                flags=re.DOTALL,
            )
            if req_match:
                for line in req_match.group(1).splitlines():
                    m_num = re.match(r"^\s*\d+\.\s+(.*)$", line)
                    if m_num:
                        req_item = m_num.group(1).strip()
                        if req_item and req_item not in all_requests:
                            all_requests.append(req_item)
            continue

        source, step_type, _ = _classify_message_source_and_type(msg)
        if source == "USER_EXPLICIT" and step_type == "USER_INPUT":
            compact_type, instructions = parse_compact_request(text)
            if compact_type == CompactRequestType.COMPACT_NO_ARGS:
                continue
            cleaned_req = instructions if compact_type == CompactRequestType.COMPACT_WITH_INSTRUCTIONS else text
            if prior_objective is None:
                prior_objective = cleaned_req
            all_requests.append(cleaned_req)

    recent = all_requests[-max_requests:] if max_requests > 0 else all_requests
    if not recent:
        return prior_objective, []

    byte_lengths = [len(r.encode("utf-8", errors="replace")) for r in recent]
    budgets = get_water_fill_allocation(byte_lengths, max_total_bytes)
    allocated_requests: list[str] = []
    for req_str, budget in zip(recent, budgets, strict=False):
        if budget > 0:
            truncated_req, _, _, _ = middle_truncate_bytes(
                req_str,
                budget,
                marker_format=" ... [truncated {omitted} bytes] ... ",
            )
            allocated_requests.append(truncated_req)

    return prior_objective, allocated_requests


def compute_trajectory_file_interactions(
    messages: Sequence[Any],
    *,
    max_items: int = DEFAULT_MAX_FILE_DIFF_ITEMS,
) -> list[str]:
    """Extract accumulated list of files viewed, edited, or written across the trajectory.

    - Carries forward any file paths already listed in a previous ``<CONTEXT_SUMMARY>``'s
      ``# Code Interaction Summary:`` section.
    - Scans tool call arguments across ``messages`` for file path keys
      (``file_path``, ``path``, ``TargetFile``, ``AbsolutePath``, ``target_file``, ``paths``).
    """
    seen: dict[str, str] = {}

    for msg in messages:
        text = _extract_message_text(msg)
        if _is_context_summary_message(msg) and "# Code Interaction Summary:" in text:
            sec_match = re.search(
                r"# Code Interaction Summary:.*?\n((?:\s*-\s+[^\n]+\n?)+)",
                text,
                flags=re.DOTALL,
            )
            if sec_match:
                for line in sec_match.group(1).splitlines():
                    m_file = re.match(r"^\s*-\s+`?([^`\n]+?)`?(?:\s+\((edited|viewed|interacted)\))?\s*$", line)
                    if m_file:
                        fpath = m_file.group(1).strip()
                        action = m_file.group(2) or "interacted"
                        if fpath:
                            seen[fpath] = action

        for tc in _extract_message_tool_calls(msg):
            name = tc.get("name", "").lower()
            args = tc.get("args", {})
            if isinstance(args, str):
                with contextlib.suppress(Exception):
                    args = json.loads(args)
            if not isinstance(args, dict):
                continue

            is_edit = any(k in name for k in ("write", "edit", "replace", "create", "patch"))
            action_label = "edited" if is_edit else "viewed"

            for key in ("file_path", "path", "TargetFile", "AbsolutePath", "target_file", "filepath"):
                val = args.get(key)
                if isinstance(val, str) and val.strip():
                    p = val.strip()
                    if seen.get(p) != "edited":
                        seen[p] = action_label
            paths_val = args.get("paths")
            if isinstance(paths_val, list):
                for item in paths_val:
                    if isinstance(item, str) and item.strip():
                        p = item.strip()
                        if seen.get(p) != "edited":
                            seen[p] = action_label

    items = [f"- `{path}` ({mode})" for path, mode in seen.items()]
    if max_items > 0 and len(items) > max_items:
        items = items[-max_items:]
    return items


def collect_artifact_snapshots(
    thread_id: str | None = "default",
    *,
    data_dir: Path | None = None,
) -> list[dict[str, str]]:
    """Collect snapshots of artifacts in ``<data_dir>/brain/<thread_id>``."""
    base = data_dir if data_dir is not None else paths.DATA_DIR
    brain_dir = base / "brain" / _sanitize_thread_id(thread_id)
    if not brain_dir.exists() or not brain_dir.is_dir():
        return []

    snapshots: list[dict[str, str]] = []
    with contextlib.suppress(Exception):
        for entry in sorted(brain_dir.iterdir()):
            if entry.name.startswith(".") or entry.name == "scratch":
                continue
            if entry.is_file():
                stat = entry.stat()
                mtime_iso = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                snapshots.append(
                    {
                        "name": entry.stem,
                        "path": f"file://{entry.resolve()}",
                        "last_edited": mtime_iso,
                    }
                )
    return snapshots


def collect_subagent_and_task_snapshots(
    messages: Sequence[Any],
    *,
    max_subagents: int = DEFAULT_MAX_SUBAGENTS,
) -> tuple[list[str], list[str]]:
    """Extract spawned subagents and running background tasks from trajectory tool calls."""
    subagents: list[str] = []
    running_tasks: list[str] = []

    for msg in messages:
        for tc in _extract_message_tool_calls(msg):
            name = tc.get("name", "")
            args = tc.get("args", {})
            if isinstance(args, str):
                with contextlib.suppress(Exception):
                    args = json.loads(args)
            if not isinstance(args, dict):
                args = {}

            if name in ("task", "invoke_subagent", "delegate_task"):
                sub_name = (
                    args.get("subagent_type")
                    or args.get("agent")
                    or args.get("role")
                    or args.get("name")
                    or "subagent"
                )
                desc = args.get("description") or args.get("prompt") or ""
                desc_short, _, _, _ = middle_truncate_bytes(
                    str(desc).replace("\n", " "),
                    160,
                    marker_format="...",
                )
                subagents.append(f"- `{sub_name}` (call_id: `{tc.get('id', 'n/a')}`): {desc_short}")
            elif name in ("run_command", "bash", "execute") and args.get("IsDaemon") is True:
                cmd_str = str(args.get("CommandLine") or args.get("command") or "")
                running_tasks.append(f"- Daemon command (call_id: `{tc.get('id', 'n/a')}`): `{cmd_str[:120]}`")

    if max_subagents > 0 and len(subagents) > max_subagents:
        subagents = subagents[-max_subagents:]
    return subagents, running_tasks


def generate_fallback_summary(
    messages: Sequence[Any],
    *,
    custom_instructions: str = "",
    max_content_bytes: int = DEFAULT_MAX_STEP_SUMMARY_BYTES,
    max_tool_arg_bytes: int = DEFAULT_MAX_TOOL_ARG_BYTES,
) -> str:
    """Generate a deterministic 6-section fallback summary + ``<COMPACTED_HISTORY>`` step log.

    When LLM summarization is unavailable or fails, produces a structured
    ``<summary>...</summary>`` containing the 6 continuation sections plus a
    one-line-per-step ``step_to_summary_json`` action log so no information is ever lost.
    """
    if not messages:
        return "<summary>\nNo steps to summarize.\n</summary>"

    objective, user_reqs = capture_user_requests(messages)
    file_interactions = compute_trajectory_file_interactions(messages)
    step_logs: list[str] = []
    tool_actions: list[str] = []
    prior_summaries: list[str] = []

    for idx, msg in enumerate(messages, start=1):
        text = _extract_message_text(msg)
        if _is_context_summary_message(msg):
            m_sum = re.search(r"<summary>(.*?)</summary>", text, flags=re.DOTALL)
            if m_sum:
                prior_summaries.append(m_sum.group(1).strip())
            continue

        step_logs.append(
            step_to_summary_json(
                msg,
                idx,
                max_content_bytes=max_content_bytes,
                max_tool_arg_bytes=max_tool_arg_bytes,
                truncate=True,
            )
        )
        for tc in _extract_message_tool_calls(msg):
            tc_entry, _ = build_tool_call_summary(tc, max_arg_bytes=max_tool_arg_bytes, truncate=True)
            tool_actions.append(f"- `{tc_entry['name']}({tc_entry['arguments_json']})`")

    obj_text = objective or (user_reqs[0] if user_reqs else "Continue assisting the user with the active session.")
    constraints_text = (
        f"On-demand compaction instructions: {custom_instructions}"
        if custom_instructions
        else "Maintain full continuity with prior user requests and workspace state."
    )
    progress_lines = []
    if prior_summaries:
        progress_lines.append(f"- Carried forward prior checkpoint summary ({len(prior_summaries)} checkpoint(s)).")
    progress_lines.append(f"- Processed {len(messages)} conversation steps prior to this checkpoint.")
    if tool_actions:
        progress_lines.append("- Executed tool actions:\n  " + "\n  ".join(tool_actions[-15:]))
    if file_interactions:
        progress_lines.append("- Interacted with files:\n  " + "\n  ".join(file_interactions[-15:]))

    compacted_history_block = "\n".join(step_logs[-25:])

    return (
        "<summary>\n"
        "## 1. Task Overview\n"
        f"- **Core Request**: {obj_text}\n"
        f"- **Scope & Instructions**: {constraints_text}\n\n"
        "## 2. Progress\n"
        + "\n".join(progress_lines)
        + "\n\n"
        "## 3. Key Findings\n"
        "- Full untruncated step payloads are preserved on disk in `transcript_full.jsonl` and compact summaries in `transcript.jsonl`.\n\n"
        "## 4. Active Context\n"
        "- Compacted step action log (`<COMPACTED_HISTORY>`):\n"
        "<COMPACTED_HISTORY>\n"
        f"{compacted_history_block}\n"
        "</COMPACTED_HISTORY>\n\n"
        "## 5. Next Steps\n"
        "- Resume from the most recent user request and inspect `transcript.jsonl` / `transcript_full.jsonl` if earlier turn details are needed.\n\n"
        "## 6. Commitments & Constraints\n"
        f"- {constraints_text}\n"
        "</summary>"
    )


def _normalize_summary_tags(raw_summary: str) -> str:
    stripped = raw_summary.strip()
    if "<summary>" in stripped and "</summary>" in stripped:
        return stripped
    return f"<summary>\n{stripped}\n</summary>"


def generate_llm_continuation_summary(
    model: BaseChatModel | None,
    messages: Sequence[Any],
    *,
    custom_instructions: str = "",
    config: CheckpointConfig | None = None,
) -> str:
    """Synchronously generate a 6-section continuation summary via the LLM with fallback."""
    if model is None or not messages:
        return generate_fallback_summary(messages, custom_instructions=custom_instructions)

    cfg = config or CheckpointConfig()
    prompt_text = CONTINUATION_SUMMARY_PROMPT
    if custom_instructions:
        prompt_text = (
            f"{prompt_text}{ON_DEMAND_COMPACTION_SUFFIX}\n\n"
            f"User Compaction Instructions: {custom_instructions}"
        )

    cleaned_window = ensure_not_starting_with_tool_call(remove_unpaired_tool_calls_and_results(messages))
    req_messages: list[BaseMessage] = [
        SystemMessage(content=CHECKPOINT_SYSTEM_PROMPT),
        *[
            m if isinstance(m, BaseMessage) else HumanMessage(content=_extract_message_text(m))
            for m in cleaned_window
        ],
        HumanMessage(content=prompt_text),
    ]

    max_attempts = max(1, cfg.retry_config.max_attempts)
    feedback_suffix = ""
    for attempt in range(1, max_attempts + 1):
        try:
            call_messages = list(req_messages)
            if feedback_suffix:
                call_messages[-1] = HumanMessage(content=f"{prompt_text}\n\n{feedback_suffix}")
            response = model.invoke(call_messages)
            tcs = _extract_message_tool_calls(response)
            if tcs:
                tc_name = tcs[0].get("name", "unknown")
                feedback_suffix = (
                    f"invalid tool call error (checkpoint_tool_call): model called tool {tc_name!r} "
                    "during context summarization. Do NOT call any tools; reply ONLY with the <summary>."
                )
                logger.warning(
                    "Context summarization attempt %d/%d produced invalid output, retrying with feedback: %s",
                    attempt,
                    max_attempts,
                    feedback_suffix,
                )
                continue

            summary_text = _extract_message_text(response).strip()
            if not summary_text:
                feedback_suffix = (
                    "invalid tool call error (checkpoint_empty_summary): model returned empty summary "
                    "during context summarization. Provide a complete 6-section <summary>."
                )
                continue

            return _normalize_summary_tags(summary_text)
        except Exception as exc:
            logger.warning("LLM checkpoint generation attempt %d/%d failed: %s", attempt, max_attempts, exc)
            break

    return generate_fallback_summary(messages, custom_instructions=custom_instructions)


async def agenerate_llm_continuation_summary(
    model: BaseChatModel | None,
    messages: Sequence[Any],
    *,
    custom_instructions: str = "",
    config: CheckpointConfig | None = None,
) -> str:
    """Asynchronously generate a 6-section continuation summary via the LLM with fallback."""
    if model is None or not messages:
        return generate_fallback_summary(messages, custom_instructions=custom_instructions)

    cfg = config or CheckpointConfig()
    prompt_text = CONTINUATION_SUMMARY_PROMPT
    if custom_instructions:
        prompt_text = (
            f"{prompt_text}{ON_DEMAND_COMPACTION_SUFFIX}\n\n"
            f"User Compaction Instructions: {custom_instructions}"
        )

    cleaned_window = ensure_not_starting_with_tool_call(remove_unpaired_tool_calls_and_results(messages))
    req_messages: list[BaseMessage] = [
        SystemMessage(content=CHECKPOINT_SYSTEM_PROMPT),
        *[
            m if isinstance(m, BaseMessage) else HumanMessage(content=_extract_message_text(m))
            for m in cleaned_window
        ],
        HumanMessage(content=prompt_text),
    ]

    max_attempts = max(1, cfg.retry_config.max_attempts)
    feedback_suffix = ""
    for attempt in range(1, max_attempts + 1):
        try:
            call_messages = list(req_messages)
            if feedback_suffix:
                call_messages[-1] = HumanMessage(content=f"{prompt_text}\n\n{feedback_suffix}")
            response = await model.ainvoke(call_messages)
            tcs = _extract_message_tool_calls(response)
            if tcs:
                tc_name = tcs[0].get("name", "unknown")
                feedback_suffix = (
                    f"invalid tool call error (checkpoint_tool_call): model called tool {tc_name!r} "
                    "during context summarization. Do NOT call any tools; reply ONLY with the <summary>."
                )
                logger.warning(
                    "Context summarization attempt %d/%d produced invalid output, retrying with feedback: %s",
                    attempt,
                    max_attempts,
                    feedback_suffix,
                )
                continue

            summary_text = _extract_message_text(response).strip()
            if not summary_text:
                feedback_suffix = (
                    "invalid tool call error (checkpoint_empty_summary): model returned empty summary "
                    "during context summarization. Provide a complete 6-section <summary>."
                )
                continue

            return _normalize_summary_tags(summary_text)
        except Exception as exc:
            logger.warning("Async LLM checkpoint generation attempt %d/%d failed: %s", attempt, max_attempts, exc)
            break

    return generate_fallback_summary(messages, custom_instructions=custom_instructions)


def build_context_summary_envelope(
    messages: Sequence[Any],
    session_summary: str,
    *,
    thread_id: str | None = "default",
    data_dir: Path | None = None,
    config: CheckpointConfig | None = None,
    generated_at: str | None = None,
) -> str:
    """Format the structured ``<CONTEXT_SUMMARY>`` checkpoint envelope.

    Includes:
    1. Generation timestamp (``This summary was generated at <ISO-8601>.``)
    2. ``# USER Objective:``
    3. ``# User Requests`` (chronological numbered list with water-fill byte allocation)
    4. ``# Previous Session Summary:`` (``<summary>...</summary>``)
    5. ``# Code Interaction Summary:`` (accumulated viewed/edited files)
    6. ``# Artifacts`` (artifact snapshots in ``<data_dir>/brain/<thread_id>``)
    7. ``# Subagents`` and ``# Running Background Tasks``
    8. ``# Conversation Logs`` (pointers to ``transcript.jsonl`` and ``transcript_full.jsonl``)
    """
    cfg = config or CheckpointConfig()
    ts = generated_at or datetime.now(timezone.utc).isoformat()
    sections: list[str] = [f"This summary was generated at {ts}."]

    objective, user_requests = capture_user_requests(
        messages,
        max_requests=cfg.max_user_requests,
        max_total_bytes=cfg.max_user_request_bytes,
    )
    if objective:
        sections.append(f"# USER Objective:\n{objective}")

    if user_requests:
        numbered = "\n".join(f"{idx}. {req}" for idx, req in enumerate(user_requests, start=1))
        sections.append(
            "# User Requests\n"
            "The following were the most recent user requests in chronological order:\n"
            f"{numbered}"
        )

    normalized_summary = _normalize_summary_tags(session_summary)
    sections.append(f"# Previous Session Summary:\n{normalized_summary}")

    file_interactions = compute_trajectory_file_interactions(
        messages,
        max_items=cfg.max_file_diff_items,
    )
    if file_interactions:
        sections.append(CODE_INTERACTION_SUMMARY_TEMPLATE % "\n".join(file_interactions))

    artifacts = collect_artifact_snapshots(thread_id, data_dir=data_dir)
    if artifacts:
        art_blocks = [
            f"[ARTIFACT: {a['name']}]\nPath: {a['path']}\nLast Edited: {a['last_edited']}"
            for a in artifacts
        ]
        sections.append(
            f"# Artifacts\nYou have the {len(artifacts)} following artifacts written to the artifacts directory:\n"
            + "\n".join(art_blocks)
        )

    subagents, running_tasks = collect_subagent_and_task_snapshots(
        messages,
        max_subagents=cfg.max_subagents,
    )
    if subagents:
        sections.append(
            SUBAGENTS_FOOTER_TEMPLATE
            % (
                f"# Subagents ({len(subagents)} total)",
                "The following subagents were spawned during this conversation.",
                "\n".join(subagents),
            )
        )
    if running_tasks:
        sections.append(RUNNING_BACKGROUND_TASKS_TEMPLATE % "\n".join(running_tasks))

    compact_path = get_transcript_path(thread_id, data_dir)
    full_path = get_full_transcript_path(thread_id, data_dir)
    sections.append(CONVERSATION_LOGS_FOOTER_TEMPLATE % (compact_path, full_path))

    body = "\n\n".join(sections)
    return CONTEXT_SUMMARY_WRAPPER % body


# ==============================================================================
# OpsCloud Compaction Middleware & Runtime Helpers
# ==============================================================================


def _offload_seed_message_id(tool_call_id: str) -> str:
    return f"{_OFFLOAD_SEED_ID_PREFIX}{tool_call_id}"


def _without_offload_seed(messages: list[Any], tool_call_id: str) -> list[Any]:
    if not tool_call_id:
        return messages
    seed_id = _offload_seed_message_id(tool_call_id)
    return [
        message
        for message in messages
        if (message.get("id") if isinstance(message, dict) else getattr(message, "id", None)) != seed_id
    ]


class RuntimeModelConfig(NamedTuple):
    """Active model configuration read from a tool runtime."""

    model_spec: str | None
    model_params: dict[str, Any]
    profile_overrides: dict[str, Any]
    context_limit: int | None


def _runtime_model_config(runtime: ToolRuntime[Any, Any]) -> RuntimeModelConfig:
    """Read the active model configuration from a tool runtime."""
    context = runtime.context
    if hasattr(context, "model_params"):
        return RuntimeModelConfig(
            model_spec=getattr(context, "model", None),
            model_params=getattr(context, "model_params", {}),
            profile_overrides=getattr(context, "profile_overrides", {}),
            context_limit=getattr(context, "model_context_limit", None),
        )
    if isinstance(context, dict):
        model = context.get("model")
        params = context.get("model_params")
        profile_overrides = context.get("profile_overrides")
        context_limit = context.get("model_context_limit")
        return RuntimeModelConfig(
            model_spec=model if isinstance(model, str) else None,
            model_params=dict(params) if isinstance(params, dict) else {},
            profile_overrides=(dict(profile_overrides) if isinstance(profile_overrides, dict) else {}),
            context_limit=context_limit if isinstance(context_limit, int) else None,
        )
    return RuntimeModelConfig(model_spec=None, model_params={}, profile_overrides={}, context_limit=None)


def _offload_tool_call_id(context: object) -> str | None:
    """Extract offload tool call ID from context if present."""
    if hasattr(context, "offload_tool_call_id"):
        value = getattr(context, "offload_tool_call_id", None)
    elif isinstance(context, dict):
        value = context.get("offload_tool_call_id")
    else:
        value = None
    return value if isinstance(value, str) and value else None


def _resolve_thread_id_from_runtime(runtime: Any, state: Any = None) -> str:
    if runtime is not None:
        ctx = getattr(runtime, "context", None)
        if isinstance(ctx, dict) and ctx.get("thread_id"):
            return str(ctx["thread_id"])
        if hasattr(ctx, "thread_id") and getattr(ctx, "thread_id", None):
            return str(getattr(ctx, "thread_id"))
        cfg = getattr(runtime, "config", None)
        if isinstance(cfg, dict):
            configurable = cfg.get("configurable", {})
            if isinstance(configurable, dict) and configurable.get("thread_id"):
                return str(configurable["thread_id"])
    if isinstance(state, dict) and state.get("_summarization_session_id"):
        return str(state["_summarization_session_id"])
    return "default"


class _ArchiveReadGuard:
    """Context manager preventing concurrent reads during archive compaction."""

    def __init__(self, backend: BackendProtocol) -> None:
        """Initialize _ArchiveReadGuard wrapping the given storage backend."""
        self._backend = backend
        self._read_failed = False

    def _record_response_errors(self, responses: list[FileDownloadResponse]) -> list[FileDownloadResponse]:
        if any(response.error is not None and response.error != FILE_NOT_FOUND for response in responses):
            self._read_failed = True
        return responses

    def _ensure_read_succeeded(self) -> None:
        if self._read_failed:
            msg = "archive read failed; refusing to overwrite existing history"
            raise RuntimeError(msg)

    def download_files(self, paths_list: list[str]) -> list[FileDownloadResponse]:
        """Download multiple files synchronously while tracking read failure states."""
        try:
            responses = self._backend.download_files(paths_list)
        except Exception:
            self._read_failed = True
            raise
        return self._record_response_errors(responses)

    async def adownload_files(self, paths_list: list[str]) -> list[FileDownloadResponse]:
        """Download multiple files asynchronously while tracking read failure states."""
        try:
            responses = await self._backend.adownload_files(paths_list)
        except Exception:
            self._read_failed = True
            raise
        return self._record_response_errors(responses)

    def write(self, file_path: str, content: str) -> WriteResult:
        """Write content synchronously only if preceding archive reads succeeded."""
        self._ensure_read_succeeded()
        return self._backend.write(file_path, content)

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        """Write content asynchronously only if preceding archive reads succeeded."""
        self._ensure_read_succeeded()
        return await self._backend.awrite(file_path, content)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """Edit file content synchronously only if preceding archive reads succeeded."""
        self._ensure_read_succeeded()
        return self._backend.edit(file_path, old_string, new_string, replace_all=replace_all)

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """Edit file content asynchronously only if preceding archive reads succeeded."""
        self._ensure_read_succeeded()
        return await self._backend.aedit(file_path, old_string, new_string, replace_all=replace_all)


def _resolve_session_id(summarization: Any, state: Any) -> str:
    getter = getattr(summarization, "_get_session_id", None)
    if callable(getter) and state is not None:
        try:
            return str(getter(state))
        except Exception:
            pass
    if isinstance(state, dict) and "_summarization_session_id" in state:
        val = state.get("_summarization_session_id")
        if val:
            return str(val)
    return f"session_{uuid4().hex}"


def _offload_to_backend_compat(
    summarization: Any,
    backend: BackendProtocol,
    to_summarize: list[Any],
    session_id: str,
) -> str | None:
    try:
        return summarization._offload_to_backend(backend, to_summarize, session_id)
    except TypeError:
        return summarization._offload_to_backend(backend, to_summarize)


async def _aoffload_to_backend_compat(
    summarization: Any,
    backend: BackendProtocol,
    to_summarize: list[Any],
    session_id: str,
) -> str | None:
    try:
        return await summarization._aoffload_to_backend(backend, to_summarize, session_id)
    except TypeError:
        return await summarization._aoffload_to_backend(backend, to_summarize)


def _build_compact_result_compat(
    middleware: Any,
    runtime: ToolRuntime[Any, Any],
    to_summarize: list[Any],
    summary: str,
    file_path: str | None,
    event: Any,
    cutoff: int,
    session_id: str,
) -> Command[Any]:
    try:
        return middleware._build_compact_result(runtime, to_summarize, summary, file_path, event, cutoff, session_id)
    except TypeError:
        return middleware._build_compact_result(runtime, to_summarize, summary, file_path, event, cutoff)


class OpsCloudSummarizationMiddleware(SummarizationMiddleware):
    """Subclass of ``SummarizationMiddleware`` implementing OpsCloud's checkpoint engine.

    Ensures automatic middleware compaction and ``compact_conversation`` tool calls use:
    - The 6-section ``CHECKPOINT_SYSTEM_PROMPT`` + ``CONTINUATION_SUMMARY_PROMPT``
      with tool-call retry guard and ``<COMPACTED_HISTORY>`` fallback.
    - Full ``<CONTEXT_SUMMARY>`` envelope formatting (``build_context_summary_envelope``).
    - Tool-call/result pairing invariants on the retained message window.
    """

    def __init__(
        self,
        *args: Any,
        checkpoint_config: CheckpointConfig | None = None,
        active_thread_id: str = "default",
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._checkpoint_config = checkpoint_config or CheckpointConfig()
        self._active_thread_id = active_thread_id

    def _partition_messages(
        self,
        conversation_messages: list[Any],
        cutoff_index: int,
    ) -> tuple[list[Any], list[Any]]:
        to_summarize, preserved = super()._partition_messages(conversation_messages, cutoff_index)
        repaired_preserved = ensure_not_starting_with_tool_call(
            remove_unpaired_tool_calls_and_results(preserved)
        )
        return to_summarize, repaired_preserved

    def _create_summary(self, messages_to_summarize: list[Any]) -> str:
        model = getattr(self._lc_helper, "model", None)
        raw_summary = generate_llm_continuation_summary(
            model,
            messages_to_summarize,
            config=self._checkpoint_config,
        )
        return build_context_summary_envelope(
            messages_to_summarize,
            raw_summary,
            thread_id=self._active_thread_id,
            config=self._checkpoint_config,
        )

    async def _acreate_summary(self, messages_to_summarize: list[Any]) -> str:
        model = getattr(self._lc_helper, "model", None)
        raw_summary = await agenerate_llm_continuation_summary(
            model,
            messages_to_summarize,
            config=self._checkpoint_config,
        )
        return build_context_summary_envelope(
            messages_to_summarize,
            raw_summary,
            thread_id=self._active_thread_id,
            config=self._checkpoint_config,
        )

    def _build_new_messages_with_path(self, summary: str, file_path: str | None) -> list[Any]:
        text = summary.strip()
        if not text.startswith("<CONTEXT_SUMMARY>"):
            text = build_context_summary_envelope(
                [],
                text,
                thread_id=self._active_thread_id,
                config=self._checkpoint_config,
            )
        if file_path is not None and file_path not in text and "</CONTEXT_SUMMARY>" in text:
            text = text.replace(
                "</CONTEXT_SUMMARY>",
                f"- Backend History Archive: `{file_path}`\n</CONTEXT_SUMMARY>",
            )
        return [
            HumanMessage(
                content=text,
                additional_kwargs={"lc_source": "summarization"},
            )
        ]


def _create_opscloud_summarization_middleware(
    model: str | BaseChatModel,
    backend: BackendProtocol,
    *,
    thread_id: str = "default",
) -> OpsCloudSummarizationMiddleware:
    """Create an ``OpsCloudSummarizationMiddleware`` honoring model profile & ``autoCompact``."""
    from deepagents._models import resolve_model  # noqa: PLC0415
    from deepagents.middleware.summarization import compute_summarization_defaults  # noqa: PLC0415

    resolved_model = resolve_model(model) if isinstance(model, str) else model
    defaults = compute_summarization_defaults(resolved_model)

    profile = getattr(resolved_model, "profile", None)
    max_input_tokens = (
        int(profile["max_input_tokens"])
        if isinstance(profile, dict) and isinstance(profile.get("max_input_tokens"), int)
        else 128_000
    )
    max_output_tokens = (
        int(profile["max_output_tokens"])
        if isinstance(profile, dict) and isinstance(profile.get("max_output_tokens"), int)
        else 16_384
    )
    checkpoint_cfg = get_checkpoint_config_for_model_info(
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
    )

    trigger = defaults["trigger"]
    effective_override = os.environ.get("OPSCLOUD_AUTO_COMPACT", "").strip()
    if not effective_override:
        with contextlib.suppress(Exception):
            effective_override = get_cli_settings_manager().get_settings().auto_compact
    if effective_override:
        normalized, ok = auto_compact_override(effective_override)
        if ok and checkpoint_cfg.max_token_limit > 0:
            trigger = ("tokens", checkpoint_cfg.max_token_limit)

    return OpsCloudSummarizationMiddleware(
        model=resolved_model,
        backend=backend,
        trigger=trigger,
        keep=defaults["keep"],
        trim_tokens_to_summarize=None,
        truncate_args_settings=defaults["truncate_args_settings"],
        checkpoint_config=checkpoint_cfg,
        active_thread_id=thread_id,
    )


class CLICompactionMiddleware(SummarizationToolMiddleware):
    """Add lossless transcript logging, tool output offloading, and conversation compaction."""

    @property
    def name(self) -> str:
        """Return the canonical middleware identifier."""
        return "SummarizationMiddleware"

    @staticmethod
    def _offload_rejection(request: ToolCallRequest) -> ToolMessage | None:
        expected_id = _offload_tool_call_id(request.runtime.context)
        if expected_id is None:
            return None

        tool_call = request.tool_call
        args = tool_call.get("args")
        messages = request.state.get("messages", [])
        last_message = messages[-1] if messages else None
        last_message_id = (
            last_message.get("id") if isinstance(last_message, dict) else getattr(last_message, "id", None)
        )
        is_seeded_compaction = (
            tool_call.get("id") == expected_id
            and tool_call.get("name") == "compact_conversation"
            and isinstance(args, dict)
            and args.get("force") is True
            and last_message_id == _offload_seed_message_id(expected_id)
        )
        if is_seeded_compaction:
            return None

        return ToolMessage(
            content=("Not executed: /offload only authorizes its seeded conversation compaction call."),
            name=tool_call.get("name", "unknown"),
            tool_call_id=tool_call.get("id", ""),
            status="error",
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        """Wrap synchronous tool call, enforce /offload guard, and offload oversized tool outputs."""
        if (rejection := self._offload_rejection(request)) is not None:
            return rejection
        result = handler(request)
        if isinstance(result, ToolMessage):
            thread_id = _resolve_thread_id_from_runtime(request.runtime, request.state)
            step_index = len(request.state.get("messages", [])) + 1
            return truncate_and_offload_tool_output(result, thread_id=thread_id, step_index=step_index)
        return result

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        """Wrap asynchronous tool call, enforce /offload guard, and offload oversized tool outputs."""
        if (rejection := self._offload_rejection(request)) is not None:
            return rejection
        result = await handler(request)
        if isinstance(result, ToolMessage):
            thread_id = _resolve_thread_id_from_runtime(request.runtime, request.state)
            step_index = len(request.state.get("messages", [])) + 1
            return truncate_and_offload_tool_output(result, thread_id=thread_id, step_index=step_index)
        return result

    def wrap_model_call(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Sync dual transcripts, run tier-1 microcompaction & read dedup, and run automatic summarization."""
        from opscloud.middleware.btw import is_btw_turn

        runtime = getattr(request, "runtime", None)
        if is_btw_turn(request.state, runtime):
            return handler(request)

        thread_id = _resolve_thread_id_from_runtime(runtime, request.state)
        with contextlib.suppress(Exception):
            messages = request.state.get("messages", []) if isinstance(request.state, dict) else []
            if messages:
                sync_messages_to_transcript(messages, thread_id=thread_id)

        req_messages = list(getattr(request, "messages", []) or [])
        if req_messages:
            hygiened = microcompact_tool_messages(deduplicate_unchanged_file_reads(req_messages))
            if hygiened != req_messages and hasattr(request, "override"):
                request = request.override(messages=hygiened)

        call_model = partial(super().wrap_model_call, handler=handler)
        summarization = self._summarization_for_runtime(runtime) if runtime is not None else self._summarization
        if hasattr(summarization, "_active_thread_id"):
            cast(Any, summarization)._active_thread_id = thread_id
        return cast(ModelResponse, summarization.wrap_model_call(request, call_model))

    async def awrap_model_call(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Sync dual transcripts, run tier-1 microcompaction & read dedup, and run automatic summarization (async)."""
        from opscloud.middleware.btw import is_btw_turn

        runtime = getattr(request, "runtime", None)
        if is_btw_turn(request.state, runtime):
            return await handler(request)

        thread_id = _resolve_thread_id_from_runtime(runtime, request.state)
        with contextlib.suppress(Exception):
            messages = request.state.get("messages", []) if isinstance(request.state, dict) else []
            if messages:
                await asyncio.to_thread(sync_messages_to_transcript, messages, thread_id)

        req_messages = list(getattr(request, "messages", []) or [])
        if req_messages:
            hygiened = microcompact_tool_messages(deduplicate_unchanged_file_reads(req_messages))
            if hygiened != req_messages and hasattr(request, "override"):
                request = request.override(messages=hygiened)

        call_model = partial(super().awrap_model_call, handler=handler)
        if runtime is not None:
            summarization = await asyncio.to_thread(self._summarization_for_runtime, runtime)
        else:
            summarization = self._summarization
        if hasattr(summarization, "_active_thread_id"):
            cast(Any, summarization)._active_thread_id = thread_id
        return cast(ModelResponse, await summarization.awrap_model_call(request, call_model))

    def _create_compact_tool(self) -> StructuredTool:
        middleware = self

        def sync_compact(
            runtime: ToolRuntime[Any, Any],
            force: Annotated[bool, InjectedToolArg] = False,
        ) -> Command[Any]:
            """Synchronously compact older messages into a summary."""
            del force
            if _offload_tool_call_id(runtime.context) != runtime.tool_call_id:
                return middleware._run_compact(runtime)
            return middleware._run_forced_compact(runtime)

        async def async_compact(
            runtime: ToolRuntime[Any, Any],
            force: Annotated[bool, InjectedToolArg] = False,
        ) -> Command[Any]:
            """Asynchronously compact older messages into a summary."""
            del force
            if _offload_tool_call_id(runtime.context) != runtime.tool_call_id:
                return await middleware._arun_compact(runtime)
            return await middleware._arun_forced_compact(runtime)

        return StructuredTool.from_function(
            name="compact_conversation",
            description=(
                "Compact the conversation by summarizing older messages into "
                "a concise summary. Use this proactively when the conversation "
                "is getting long to free up context window space."
            ),
            func=sync_compact,
            coroutine=async_compact,
        )

    def _guarded_backend(self, runtime: ToolRuntime[Any, Any] | None = None) -> BackendProtocol:
        backend_obj = getattr(self._summarization, "_backend", None)
        if callable(backend_obj) and runtime is not None:
            backend = cast(BackendProtocol, backend_obj(runtime))
        else:
            backend = cast(BackendProtocol, backend_obj)
        return cast(BackendProtocol, _ArchiveReadGuard(backend))

    def _summarization_for_runtime(self, runtime: ToolRuntime[Any, Any] | None) -> SummarizationMiddleware:
        """Build a summarizer for the active runtime model when overridden."""
        if runtime is None:
            return self._summarization
        config = _runtime_model_config(runtime)
        if not config.model_spec:
            return self._summarization

        from opscloud.model.factory import create_model

        model = create_model(
            config.model_spec,
            extra_kwargs=config.model_params or None,
            profile_overrides=config.profile_overrides or None,
        ).model
        context_limit = config.context_limit
        if context_limit is not None:
            profile = getattr(model, "profile", None)
            native = profile.get("max_input_tokens") if isinstance(profile, dict) else None
            if native != context_limit:
                merged = (
                    {**profile, "max_input_tokens": context_limit}
                    if isinstance(profile, dict)
                    else {"max_input_tokens": context_limit}
                )
                try:
                    cast(Any, model).profile = merged
                except (AttributeError, TypeError, ValueError):
                    logger.warning(
                        "Could not apply runtime context limit %d to the offload "
                        "model profile; using its resolved profile",
                        context_limit,
                        exc_info=True,
                    )
        backend = self._guarded_backend(runtime)
        thread_id = _resolve_thread_id_from_runtime(runtime)
        return _create_opscloud_summarization_middleware(model, backend, thread_id=thread_id)

    def _run_compact(self, runtime: ToolRuntime[Any, Any]) -> Command[Any]:
        """Synchronously compact conversation via ``compact_conversation`` tool with transcript sync."""
        messages = runtime.state.get("messages", [])
        thread_id = _resolve_thread_id_from_runtime(runtime, runtime.state)
        with contextlib.suppress(Exception):
            if messages:
                sync_messages_to_transcript(messages, thread_id=thread_id)
        if hasattr(self._summarization, "_active_thread_id"):
            cast(Any, self._summarization)._active_thread_id = thread_id
        return super()._run_compact(runtime)

    async def _arun_compact(self, runtime: ToolRuntime[Any, Any]) -> Command[Any]:
        """Asynchronously compact conversation via ``compact_conversation`` tool with transcript sync."""
        messages = runtime.state.get("messages", [])
        thread_id = _resolve_thread_id_from_runtime(runtime, runtime.state)
        with contextlib.suppress(Exception):
            if messages:
                await asyncio.to_thread(sync_messages_to_transcript, messages, thread_id)
        if hasattr(self._summarization, "_active_thread_id"):
            cast(Any, self._summarization)._active_thread_id = thread_id
        return await super()._arun_compact(runtime)

    def _run_forced_compact(self, runtime: ToolRuntime[Any, Any]) -> Command[Any]:
        tool_call_id = runtime.tool_call_id or ""
        try:
            summarization = self._summarization_for_runtime(runtime)
            messages = runtime.state.get("messages", [])
            thread_id = _resolve_thread_id_from_runtime(runtime, runtime.state)
            if hasattr(summarization, "_active_thread_id"):
                cast(Any, summarization)._active_thread_id = thread_id
            with contextlib.suppress(Exception):
                sync_messages_to_transcript(messages, thread_id=thread_id)

            event = runtime.state.get("_summarization_event")
            effective = summarization._apply_event_to_messages(messages, event)
            effective = _without_offload_seed(effective, tool_call_id)
            cutoff = summarization._determine_cutoff_index(effective)
            if cutoff == 0:
                return self._nothing_to_compact(tool_call_id)

            session_id = _resolve_session_id(summarization, runtime.state)
            to_summarize, _ = summarization._partition_messages(effective, cutoff)
            raw_summary = summarization._create_summary(to_summarize)
            envelope_summary = (
                raw_summary
                if raw_summary.lstrip().startswith("<CONTEXT_SUMMARY>")
                else build_context_summary_envelope(
                    to_summarize,
                    raw_summary,
                    thread_id=thread_id,
                )
            )
            backend = self._guarded_backend(runtime)
            file_path = _offload_to_backend_compat(summarization, backend, to_summarize, session_id)
            return _build_compact_result_compat(
                self, runtime, to_summarize, envelope_summary, file_path, event, cutoff, session_id
            )
        except Exception as exc:
            logger.exception("forced compact_conversation failed")
            return self._forced_compact_error(tool_call_id, exc)

    async def _arun_forced_compact(self, runtime: ToolRuntime[Any, Any]) -> Command[Any]:
        tool_call_id = runtime.tool_call_id or ""
        try:
            summarization = await asyncio.to_thread(self._summarization_for_runtime, runtime)
            messages = runtime.state.get("messages", [])
            thread_id = _resolve_thread_id_from_runtime(runtime, runtime.state)
            if hasattr(summarization, "_active_thread_id"):
                cast(Any, summarization)._active_thread_id = thread_id
            with contextlib.suppress(Exception):
                await asyncio.to_thread(sync_messages_to_transcript, messages, thread_id)

            event = runtime.state.get("_summarization_event")
            effective = summarization._apply_event_to_messages(messages, event)
            effective = _without_offload_seed(effective, tool_call_id)
            cutoff = summarization._determine_cutoff_index(effective)
            if cutoff == 0:
                return self._nothing_to_compact(tool_call_id)

            session_id = _resolve_session_id(summarization, runtime.state)
            to_summarize, _ = summarization._partition_messages(effective, cutoff)
            raw_summary = await summarization._acreate_summary(to_summarize)
            envelope_summary = (
                raw_summary
                if raw_summary.lstrip().startswith("<CONTEXT_SUMMARY>")
                else build_context_summary_envelope(
                    to_summarize,
                    raw_summary,
                    thread_id=thread_id,
                )
            )
            backend = self._guarded_backend(runtime)
            file_path = await _aoffload_to_backend_compat(summarization, backend, to_summarize, session_id)
            return _build_compact_result_compat(
                self, runtime, to_summarize, envelope_summary, file_path, event, cutoff, session_id
            )
        except Exception as exc:
            logger.exception("forced compact_conversation failed")
            return self._forced_compact_error(tool_call_id, exc)

    @staticmethod
    def _forced_compact_error(tool_call_id: str, exc: Exception) -> Command[Any]:
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content=(
                            f"{COMPACTION_FAILURE_PREFIX}: an error occurred "
                            f"during compaction ({type(exc).__name__}: {exc}). "
                            "Your conversation is unchanged."
                        ),
                        tool_call_id=tool_call_id,
                    )
                ],
            }
        )


def _create_cli_compaction_middleware(
    model: str | BaseChatModel,
    backend: BackendProtocol,
) -> CLICompactionMiddleware:
    """Create the OpsCloud compaction middleware from the SDK configuration."""
    profile = getattr(model, "profile", None)
    if not isinstance(model, str) and (not isinstance(profile, dict) or not profile.get("max_input_tokens")):
        default_profile = {"max_input_tokens": 128_000}
        if isinstance(profile, dict):
            default_profile.update(profile)
        try:
            cast(Any, model).profile = default_profile
        except Exception:
            with contextlib.suppress(Exception):
                cast(Any, type(model)).profile = property(lambda self: dict(default_profile))
    sdk_middleware = create_summarization_tool_middleware(model, backend)
    opscloud_summarization = _create_opscloud_summarization_middleware(model, backend)
    combined_prompt = sdk_middleware.system_prompt or ""
    if CONVERSATION_TRANSCRIPT_SYSTEM_SECTION not in combined_prompt:
        combined_prompt = (
            f"{combined_prompt}\n\n{CONVERSATION_TRANSCRIPT_SYSTEM_SECTION}".strip()
            if combined_prompt
            else CONVERSATION_TRANSCRIPT_SYSTEM_SECTION
        )
    return CLICompactionMiddleware(
        opscloud_summarization,
        system_prompt=combined_prompt,
    )


CompactionMiddleware = CLICompactionMiddleware
create_cli_compaction_middleware = _create_cli_compaction_middleware

__all__ = [
    "CHECKPOINT_SYSTEM_PROMPT",
    "CODE_INTERACTION_SUMMARY_TEMPLATE",
    "COMPACTION_FAILURE_PREFIX",
    "CONTINUATION_SUMMARY_PROMPT",
    "CONTEXT_SUMMARY_WRAPPER",
    "CONVERSATION_LOGS_FOOTER_TEMPLATE",
    "CONVERSATION_TRANSCRIPT_SYSTEM_SECTION",
    "CLICompactionMiddleware",
    "CheckpointConfig",
    "CheckpointRetryConfig",
    "CompactRequestType",
    "CompactionMiddleware",
    "ON_DEMAND_COMPACTION_SUFFIX",
    "OpsCloudSummarizationMiddleware",
    "RESUMING_FROM_COMPACTION_INTRO",
    "RuntimeModelConfig",
    "_create_cli_compaction_middleware",
    "_create_opscloud_summarization_middleware",
    "agenerate_llm_continuation_summary",
    "build_context_summary_envelope",
    "build_tool_call_summary",
    "capture_user_requests",
    "clamp_checkpoint_config_for_model_info",
    "collect_artifact_snapshots",
    "collect_subagent_and_task_snapshots",
    "compute_trajectory_file_interactions",
    "create_cli_compaction_middleware",
    "deduplicate_unchanged_file_reads",
    "ensure_not_starting_with_tool_call",
    "generate_fallback_summary",
    "generate_llm_continuation_summary",
    "get_checkpoint_config_for_model_info",
    "get_default_checkpoint_config_for_model",
    "get_full_transcript_path",
    "get_step_output_directory_path",
    "get_transcript_path",
    "get_water_fill_allocation",
    "microcompact_tool_messages",
    "middle_truncate_bytes",
    "parse_compact_request",
    "remove_unpaired_tool_calls_and_results",
    "step_to_summary_json",
    "sync_messages_to_transcript",
    "truncate_and_offload_tool_output",
    "validate_checkpoint_config_for_model_info",
]
