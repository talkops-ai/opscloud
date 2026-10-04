"""Evidence extraction and compaction for Jev System One rubric grading.

Parses agent conversation state, tool calls (shell commands, file modifications),
and exit codes into a structured, bounded evidence payload (<4,000 tokens) suitable
for TypeSafe decision evaluation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
import json
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    ToolMessage,
)

from opscloud.middleware.goal_state_notice import is_conversation_control_message
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

_MAX_COMMAND_OUTPUT_CHARS = 1200
_MAX_TOTAL_EVIDENCE_CHARS = 16000
_MAX_MESSAGES_TO_SCAN = 40


def _clean_text(value: Any, max_len: int = 500) -> str:
    """Safely convert any value to string and truncate."""
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) > max_len:
        return text[:max_len] + f"... [truncated {len(text) - max_len} chars]"
    return text


def extract_grading_evidence(
    state_or_messages: Mapping[str, Any] | Sequence[Any],
    *,
    max_total_chars: int = _MAX_TOTAL_EVIDENCE_CHARS,
) -> dict[str, Any]:
    """Extract structured, bounded operational evidence for Jev evaluation.

    Args:
        state_or_messages: Agent state mapping or sequence of messages.
        max_total_chars: Total character budget for the extracted evidence payload.

    Returns:
        Structured evidence dictionary with executed commands, file edits,
        staged completion notes, and error reports.
    """
    messages: Sequence[Any] = []
    completion_note: str | None = None
    goal_note: str | None = None

    if isinstance(state_or_messages, Mapping):
        messages = state_or_messages.get("messages", []) or []
        completion_note = state_or_messages.get("_pending_goal_completion_note")
        goal_note = state_or_messages.get("_goal_status_note")
    elif isinstance(state_or_messages, Sequence) and not isinstance(state_or_messages, (str, bytes)):
        messages = state_or_messages

    commands_executed: list[dict[str, Any]] = []
    files_modified: list[dict[str, Any]] = []
    tool_operations: list[dict[str, Any]] = []
    errors_encountered: list[str] = []
    recent_dialogue: list[dict[str, str]] = []

    # Map tool_call_id to command / file metadata
    tool_call_map: dict[str, dict[str, Any]] = {}

    scan_window = list(messages)[-_MAX_MESSAGES_TO_SCAN:]

    for msg in scan_window:
        if is_conversation_control_message(msg):
            continue

        # 1. Inspect AI tool invocations
        if isinstance(msg, AIMessage) or (isinstance(msg, Mapping) and msg.get("role") == "assistant"):
            tool_calls = getattr(msg, "tool_calls", None) or (msg.get("tool_calls") if isinstance(msg, Mapping) else None)
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    tc_id = tc.get("id") or ""
                    tc_name = tc.get("name") or ""
                    tc_args = dict(tc.get("args") or {})

                    tool_call_map[tc_id] = {
                        "name": tc_name,
                        "args": tc_args,
                    }

                    if tc_name in {"execute", "execute_command", "run_command", "shell", "bash"}:
                        cmd = str(tc_args.get("command") or tc_args.get("cmd") or tc_args.get("CommandLine") or "")
                        commands_executed.append({
                            "id": tc_id,
                            "command": _clean_text(cmd, 300),
                            "status": "pending",
                            "output": "",
                        })
                    elif tc_name in {"write_file", "edit_file", "replace_file_content", "multi_replace_file_content"}:
                        path = str(tc_args.get("path") or tc_args.get("file_path") or tc_args.get("TargetFile") or "")
                        files_modified.append({
                            "path": path,
                            "operation": tc_name,
                        })
                    else:
                        # Cloud, MCP, or domain-specific tools (e.g. AWS, Kubernetes, Terraform)
                        sanitized_args = {
                            str(k): _clean_text(v, 150)
                            for k, v in list(tc_args.items())[:6]
                        }
                        tool_operations.append({
                            "id": tc_id,
                            "tool": tc_name,
                            "args": sanitized_args,
                            "status": "pending",
                            "output": "",
                        })

            # Record textual assistant explanations
            content = getattr(msg, "content", None) or (msg.get("content") if isinstance(msg, Mapping) else "")
            if isinstance(content, str) and content.strip() and not tool_calls:
                recent_dialogue.append({
                    "role": "assistant",
                    "text": _clean_text(content, 400),
                })

        # 2. Inspect Tool execution outputs & return codes
        elif isinstance(msg, ToolMessage) or (isinstance(msg, Mapping) and msg.get("role") == "tool"):
            tc_id = getattr(msg, "tool_call_id", None) or (msg.get("tool_call_id") if isinstance(msg, Mapping) else "")
            content = getattr(msg, "content", "") or (msg.get("content") if isinstance(msg, Mapping) else "")
            status = getattr(msg, "status", "success") or (msg.get("status") if isinstance(msg, Mapping) else "success")

            content_str = str(content).strip()
            is_err = status == "error" or "error" in content_str.lower()[:100]

            if is_err and len(content_str) > 0:
                errors_encountered.append(_clean_text(content_str, 300))

            # Match with command
            matched_cmd = next((c for c in commands_executed if c.get("id") == tc_id), None)
            if matched_cmd:
                matched_cmd["status"] = "error" if is_err else "success"
                matched_cmd["output"] = _clean_text(content_str, _MAX_COMMAND_OUTPUT_CHARS)

            # Match with cloud/MCP tool operation
            matched_tool = next((t for t in tool_operations if t.get("id") == tc_id), None)
            if matched_tool:
                matched_tool["status"] = "error" if is_err else "success"
                matched_tool["output"] = _clean_text(content_str, _MAX_COMMAND_OUTPUT_CHARS)

        # 3. User requests
        elif isinstance(msg, HumanMessage) or (isinstance(msg, Mapping) and msg.get("role") in {"user", "human"}):
            content = getattr(msg, "content", "") or (msg.get("content") if isinstance(msg, Mapping) else "")
            if isinstance(content, str) and content.strip():
                # Filter out internal grader injection prompts
                if getattr(msg, "name", None) != "rubric_grader":
                    recent_dialogue.append({
                        "role": "user",
                        "text": _clean_text(content, 300),
                    })

    # Assemble structured evidence payload
    evidence: dict[str, Any] = {
        "commands_executed": [
            {
                "command": c["command"],
                "status": c["status"],
                "output": c["output"],
            }
            for c in commands_executed[-10:]
        ],
        "files_modified": files_modified[-10:],
        "recent_dialogue": recent_dialogue[-5:],
    }

    if tool_operations:
        evidence["cloud_and_mcp_tools"] = [
            {
                "tool": t["tool"],
                "args": t["args"],
                "status": t["status"],
                "output": t["output"],
            }
            for t in tool_operations[-10:]
        ]

    if completion_note:
        evidence["completion_evidence_note"] = _clean_text(completion_note, 1000)
    if goal_note:
        evidence["goal_status_note"] = _clean_text(goal_note, 500)
    if errors_encountered:
        evidence["recent_errors"] = errors_encountered[-5:]

    # Enforce total character budget on the extracted evidence payload
    try:
        raw_len = len(json.dumps(evidence))
        if raw_len > max_total_chars:
            # Compact verbose outputs to strictly guarantee budget compliance
            for c in evidence["commands_executed"]:
                if len(c["output"]) > 300:
                    c["output"] = c["output"][:300] + "... [truncated]"
            if "cloud_and_mcp_tools" in evidence:
                for t in evidence["cloud_and_mcp_tools"]:
                    if len(t["output"]) > 300:
                        t["output"] = t["output"][:300] + "... [truncated]"
            if "completion_evidence_note" in evidence and len(evidence["completion_evidence_note"]) > 500:
                evidence["completion_evidence_note"] = evidence["completion_evidence_note"][:500] + "... [truncated]"
            if "goal_status_note" in evidence and len(evidence["goal_status_note"]) > 300:
                evidence["goal_status_note"] = evidence["goal_status_note"][:300] + "... [truncated]"
    except Exception:
        pass

    logger.debug(
        "extract_grading_evidence: gathered %d commands, %d modified files, %d tool operations, %d errors",
        len(evidence["commands_executed"]),
        len(evidence["files_modified"]),
        len(evidence.get("cloud_and_mcp_tools", [])),
        len(errors_encountered),
    )

    return evidence


__all__ = ["extract_grading_evidence"]
