"""Jev-powered System One tool-call safety classifier.

Evaluates semantic blast radius and mutation likelihood in <100ms using
TypeSafe AI Jev and LangChain-native decision primitives (Noul and Score).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
import os
from pathlib import Path
import re
import time
from typing import Any

from langchain_core.messages import ToolCall
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from opscloud.utils.logger import get_logger

logger = get_logger(__name__)


class JevToolVerdict(BaseModel):
    """Calibrated safety evaluation verdict produced by Jev System One."""

    tool_call_id: str
    tool_name: str
    is_mutating: bool
    mutating_probability: float
    blast_radius: float
    risk_level: int = Field(description="0=Safe, 1=Controlled/Reversible, 2=Critical")
    requires_human_interrupt: bool
    rationale: str
    latency_ms: int = 0
    action_type: str = "standard_tool"
    caller_agent: str = "main"
    intent_alignment_probability: float = 1.0
    confidence: float = 1.0
    critical_probability: float = 0.0
    request_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    subagent_name: str | None = None
    mcp_server: str | None = None


class JevSecurityClassifier:
    """System One tool-call safety classifier powered by TypeSafe AI Jev."""

    def __init__(
        self,
        api_key: str | None = None,
        timeout_seconds: float = 2.0,
    ) -> None:
        """Initialize the Jev classifier with API key and timeout budget."""
        from opscloud.config.settings import get_settings, resolve_env_var
        if api_key is not None:
            resolved_key = api_key if api_key else None
            self._explicit_key = True
        else:
            settings_key = getattr(get_settings(), "typesafe_api_key", None)
            resolved_key = settings_key or resolve_env_var("TYPESAFE_API_KEY", fallback_names=("JEV_API_KEY",))
            self._explicit_key = False

        self._api_key = resolved_key
        self.timeout_seconds = timeout_seconds
        self._classifier = None

    def _get_classifier(self) -> Any:
        """Lazily initialize TypeSafeClassifier instance."""
        if self._classifier is None:
            from langchain_typesafe import TypeSafeClassifier
            if self._api_key:
                self._classifier = TypeSafeClassifier(api_key=self._api_key)
            else:
                self._classifier = TypeSafeClassifier()
        return self._classifier

    def is_available(self) -> bool:
        """Check whether TypeSafe API key or credentials are configured."""
        if self._explicit_key:
            return bool(self._api_key)
        if self._api_key:
            return True
        from opscloud.config.settings import get_settings, resolve_env_var
        settings_key = getattr(get_settings(), "typesafe_api_key", None)
        if settings_key:
            return True
        return bool(resolve_env_var("TYPESAFE_API_KEY", fallback_names=("JEV_API_KEY",)))

    def _compact_arguments(self, raw_args: dict[str, Any], max_len: int = 2000) -> dict[str, Any]:
        """Truncate massive file contents or payloads to prevent Jev context bloat (32k ceiling)."""
        compacted: dict[str, Any] = {}
        for k, v in raw_args.items():
            s = str(v)
            if len(s) > max_len:
                compacted[k] = s[:max_len] + f"... [truncated {len(s) - max_len} chars]"
            else:
                compacted[k] = v
        return compacted

    def _detect_action_type(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        tool: BaseTool | None = None,
    ) -> str:
        """Identify the operational category and provenance of the tool call."""
        normalized = tool_name.lower()
        if (
            normalized in {"task", "subagent", "delegate_task", "call_subagent", "spawn_subagent"}
            or "subagent_type" in tool_args
        ):
            return "subagent_dispatch"

        if (
            normalized.startswith("mcp__")
            or ":" in normalized
            or (
                tool is not None
                and hasattr(tool, "metadata")
                and bool(
                    (tool.metadata or {}).get("_mcp_server")
                    or (tool.metadata or {}).get("is_mcp")
                    or (tool.metadata or {}).get("server_name")
                )
            )
        ):
            return "mcp_tool"

        if normalized in {"execute", "execute_command", "run_command", "shell", "bash", "terminal", "sh"}:
            return "shell_command"

        if (
            normalized in {"run_skill", "execute_skill", "run_skill_script"}
            or any("skills/" in str(v) for v in tool_args.values())
        ):
            return "skill_execution"

        if normalized in {
            "write_file",
            "edit_file",
            "replace_file_content",
            "multi_replace_file_content",
            "create_temp_artifact",
            "delete_temp_artifact",
        }:
            return "filesystem_operation"

        return "standard_tool"

    def _compact_tool_description(self, desc: str, max_chars: int = 250) -> str:
        """Extract primary semantic purpose of the tool, omitting massive usage manuals."""
        if not desc:
            return ""
        summary = desc.split("\n\n")[0].strip()
        lines = summary.split("\n")
        clean = lines[0].strip() if lines else summary
        if len(clean) > max_chars:
            return clean[:max_chars] + "..."
        return clean

    def _extract_shell_details(self, tool_args: dict[str, Any]) -> dict[str, Any]:
        """Extract command string and pre-compute deterministic AST safety metrics."""
        raw_cmd = str(
            tool_args.get("command")
            or tool_args.get("cmd")
            or tool_args.get("CommandLine")
            or ""
        ).strip()

        is_readonly = False
        is_destructive = False
        ast_tier = 3
        ast_reason = "No command provided"
        is_skill_script = "skills/" in raw_cmd or ".opscloud/skills" in raw_cmd

        if raw_cmd:
            try:
                from opscloud.security.cli_ast_evaluator import evaluate_cli_safety

                ast_eval = evaluate_cli_safety(raw_cmd)
                is_readonly = bool(ast_eval.get("is_readonly", False))
                is_destructive = bool(ast_eval.get("is_destructive", False))
                ast_tier = int(ast_eval.get("tier", 3))
                ast_reason = str(ast_eval.get("reason", ""))
            except Exception as exc:
                ast_reason = f"AST evaluation error: {exc}"

        return {
            "command": raw_cmd[:600],
            "is_ast_readonly": is_readonly,
            "is_ast_destructive": is_destructive,
            "ast_tier": ast_tier,
            "ast_reason": ast_reason,
            "is_skill_script": is_skill_script,
        }

    def _extract_mcp_details(
        self,
        tool_name: str,
        tool: BaseTool | None = None,
    ) -> dict[str, Any]:
        """Extract MCP server ownership, description, and spec annotations."""
        server_name = ""
        clean_action = tool_name
        if tool_name.startswith("mcp__"):
            parts = tool_name.split("__")
            if len(parts) >= 2:
                server_name = parts[1]
                clean_action = "__".join(parts[2:]) if len(parts) > 2 else parts[1]
        elif ":" in tool_name:
            parts = tool_name.split(":", 1)
            server_name = parts[0]
            clean_action = parts[1]

        tool_meta = dict(tool.metadata or {}) if tool is not None and hasattr(tool, "metadata") else {}
        if not server_name and "_mcp_server" in tool_meta:
            server_name = str(tool_meta["_mcp_server"])
        if not server_name and "server_name" in tool_meta:
            server_name = str(tool_meta["server_name"])

        return {
            "server_name": server_name or "external_mcp",
            "mcp_action": clean_action,
            "read_only_hint": bool(tool_meta.get("readOnlyHint", False)),
            "destructive_hint": bool(tool_meta.get("destructiveHint", False)),
            "idempotent_hint": bool(tool_meta.get("idempotentHint", False)),
            "open_world_hint": bool(tool_meta.get("openWorldHint", False)),
        }

    def _extract_subagent_details(self, tool_args: dict[str, Any]) -> dict[str, Any]:
        """Extract target subagent role, delegated prompt/instructions, and capability boundaries."""
        target_subagent = str(
            tool_args.get("subagent_type")
            or tool_args.get("name")
            or tool_args.get("agent")
            or "subagent"
        )
        task_instruction = str(
            tool_args.get("description")
            or tool_args.get("prompt")
            or tool_args.get("instruction")
            or tool_args.get("task")
            or ""
        )

        sub_desc = ""
        allowed_tools: list[str] = []
        allowed_skills: list[str] = []
        has_exec_privilege = False
        try:
            from opscloud.subagents.loader import list_subagents

            metas = list_subagents()
            matched = next((m for m in metas if m.get("name") == target_subagent), None)
            if matched:
                sub_desc = matched.get("description", "")
                allowed_tools = list(matched.get("tools") or [])
                allowed_skills = list(matched.get("skills") or [])
                if any(t in {"execute", "run_command", "write_file", "edit_file"} for t in allowed_tools):
                    has_exec_privilege = True
            else:
                # Unrestricted or general-purpose subagent
                has_exec_privilege = True
        except Exception:
            has_exec_privilege = True

        return {
            "target_subagent": target_subagent,
            "delegated_task": task_instruction[:600],
            "subagent_description": sub_desc[:300],
            "allowed_tools": allowed_tools,
            "allowed_skills": allowed_skills,
            "has_execution_privilege": has_exec_privilege,
        }

    async def evaluate_call(
        self,
        tool_call_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        active_environment: str = "production",
        worktree_root: str = ".",
        *,
        tool: BaseTool | None = None,
        user_prompt: str = "",
        user_goal: str = "",
        caller_agent: str = "main",
        is_deep_agent: bool = False,
        active_skill: str | None = None,
    ) -> JevToolVerdict:
        """Evaluate a tool call across multi-agent, MCP, shell, and skill contexts."""
        from langchain_typesafe import Noul, NoulCriteria, Score

        action_type = self._detect_action_type(tool_name, tool_args, tool=tool)
        if not self.is_available():
            # Fail closed: missing credentials require human interrupt
            return JevToolVerdict(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                is_mutating=True,
                mutating_probability=1.0,
                blast_radius=2.0,
                risk_level=2,
                requires_human_interrupt=True,
                rationale="TypeSafe API key (TYPESAFE_API_KEY) not configured; failing closed to human confirmation.",
                action_type=action_type,
                caller_agent=caller_agent,
            )

        compact_args = self._compact_arguments(tool_args)
        is_prod = active_environment.lower() in {"prod", "production", "live"}

        # 1. Action-specific context extraction
        shell_details: dict[str, Any] | None = None
        if action_type == "shell_command":
            shell_details = self._extract_shell_details(tool_args)

        mcp_details: dict[str, Any] | None = None
        if action_type == "mcp_tool":
            mcp_details = self._extract_mcp_details(tool_name, tool=tool)

        subagent_details: dict[str, Any] | None = None
        if action_type == "subagent_dispatch":
            subagent_details = self._extract_subagent_details(tool_args)

        skill_details: dict[str, Any] | None = None
        if active_skill or action_type == "skill_execution" or (shell_details and shell_details.get("is_skill_script")):
            skill_details = {
                "active_skill": active_skill or "custom_skill",
                "invoking_tool": tool_name,
                "is_skill_script": bool(shell_details and shell_details.get("is_skill_script")),
            }

        # 2. Build multi-dimensional state payload for Jev
        state_payload: dict[str, Any] = {
            "action_type": action_type,
            "tool_name": tool_name,
            "tool_description": self._compact_tool_description(tool.description if tool and tool.description else ""),
            "tool_arguments": compact_args,
            "environment": active_environment,
            "is_production": is_prod,
            "worktree_root": worktree_root,
            "caller_context": {
                "agent_name": caller_agent,
                "is_deep_agent": is_deep_agent,
                "agent_role": "subagent" if is_deep_agent else "main_orchestrator",
            },
        }

        if shell_details:
            state_payload["shell_details"] = shell_details
        if mcp_details:
            state_payload["mcp_details"] = mcp_details
        if subagent_details:
            state_payload["subagent_details"] = subagent_details
        if skill_details:
            state_payload["skill_details"] = skill_details

        user_intent: dict[str, str] = {}
        if user_prompt:
            user_intent["literal_user_request"] = user_prompt[:400]
        if user_goal:
            user_intent["active_goal"] = user_goal[:200]
        if user_intent:
            state_payload["user_intent"] = user_intent

        # 3. Formulate calibrated questions
        questions: dict[str, Any] = {
            "is_mutating": Noul(
                instructions=(
                    "Does this action (whether via external shell command, MCP tool, "
                    "subagent delegation, skill script, or filesystem operation) alter, "
                    "create, delete, or mutate infrastructure, cloud services, cluster state, "
                    "files, or persistent system data?"
                ),
                criteria=NoulCriteria(
                    true=(
                        "Modifies live resources, writes/edits files, executes state-changing CLI commands, "
                        "scales pods/instances, alters configs, or dispatches a subagent with mutating tasks."
                    ),
                    false=(
                        "Purely read-only inspection, search, status query, log retrieval, "
                        "dry-run validation, read_file, or subagent dispatch for read-only triage."
                    ),
                ),
            ),
            "blast_radius": Score(
                instructions=(
                    "Rate the operational blast radius and hazard of this action across "
                    "cloud infrastructure, Kubernetes clusters, external APIs, and local environment."
                ),
                criteria=[
                    "Negligible: Harmless read-only inspection, status query, local temporary check with zero side-effects.",
                    "Controlled: Reversible modification, non-production staging change, safe file edit within worktree, or scoped low-risk subagent execution.",
                    "Critical: Modifies or targets production resources, terminates instances, drops database/namespace, deletes deployments, alters credentials/RBAC, force pushes, executes untrusted external scripts, or dispatches unconstrained high-privilege subagents.",
                ],
            ),
        }

        if user_intent:
            questions["intent_alignment"] = Noul(
                instructions=(
                    "Is this action directly aligned with and authorized by the user's "
                    "literal request and stated goal?"
                ),
                criteria=NoulCriteria(
                    true=(
                        "The action directly fulfills, investigates, or progresses what the user "
                        "explicitly requested without unrequested collateral side effects."
                    ),
                    false=(
                        "The action performs unrequested modifications, targets unexpected infrastructure, "
                        "escalates permissions unnecessarily, or diverges from the user's prompt."
                    ),
                ),
            )

        classifier = self._get_classifier()
        start_time = time.monotonic()
        run_config = {
            "run_name": "jev_system_one_classifier",
            "tags": ["opscloud:smart", "opscloud:jev"],
            "metadata": {
                "tool_name": tool_name,
                "action_type": action_type,
                "caller_agent": caller_agent,
                "environment": active_environment,
            },
        }
        try:
            async with asyncio.timeout(self.timeout_seconds):
                response = await classifier.ainvoke(
                    {
                        "state": state_payload,
                        "questions": questions,
                    },
                    config=run_config,
                )
        except TimeoutError:
            latency_ms = int((time.monotonic() - start_time) * 1000)
            logger.warning("Jev safety classifier timed out after %ss for %s", self.timeout_seconds, tool_name)
            return JevToolVerdict(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                is_mutating=True,
                mutating_probability=1.0,
                blast_radius=2.0,
                risk_level=2,
                requires_human_interrupt=True,
                rationale=f"Jev classifier timed out ({self.timeout_seconds}s); failing closed to human approval.",
                latency_ms=latency_ms,
                action_type=action_type,
                caller_agent=caller_agent,
            )
        except Exception as exc:
            latency_ms = int((time.monotonic() - start_time) * 1000)
            logger.warning("Jev safety classifier error for %s: %s", tool_name, exc)
            return JevToolVerdict(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                is_mutating=True,
                mutating_probability=1.0,
                blast_radius=2.0,
                risk_level=2,
                requires_human_interrupt=True,
                rationale=f"Jev classifier error ({type(exc).__name__}); failing closed to human approval.",
                latency_ms=latency_ms,
                action_type=action_type,
                caller_agent=caller_agent,
            )

        latency_ms = int((time.monotonic() - start_time) * 1000)
        p_mutating = float(response.nouls["is_mutating"].noul)

        blast_obj = response.scores.get("blast_radius") if hasattr(response, "scores") else None
        score_val = float(getattr(blast_obj, "score", 0.0)) if blast_obj else 0.0

        raw_conf = getattr(blast_obj, "confidence", None)
        confidence = float(raw_conf) if isinstance(raw_conf, (int, float)) else 1.0

        probs = getattr(blast_obj, "probabilities", None)
        if isinstance(probs, dict):
            raw_p = probs.get("2", 0.0) or probs.get(2, 0.0)
            p_critical = float(raw_p) if isinstance(raw_p, (int, float)) else 0.0
        else:
            p_critical = 0.0

        p_aligned = (
            float(response.nouls["intent_alignment"].noul)
            if "intent_alignment" in response.nouls
            else 1.0
        )

        raw_req_id = getattr(response, "request_id", None)
        req_id = raw_req_id if isinstance(raw_req_id, str) else None

        usage = getattr(response, "usage", None)
        input_tokens = None
        output_tokens = None
        if isinstance(usage, dict):
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")
        elif usage is not None and not hasattr(usage, "_mock_return_value"):
            raw_in = getattr(usage, "input_tokens", None)
            raw_out = getattr(usage, "output_tokens", None)
            input_tokens = raw_in if isinstance(raw_in, int) else None
            output_tokens = raw_out if isinstance(raw_out, int) else None

        is_mutating = p_mutating >= 0.30
        risk_level = round(score_val)

        # Build descriptive action summary for user and audit log
        subagent_name: str | None = None
        mcp_server: str | None = None
        if shell_details:
            preview = shell_details["command"][:60]
            action_summary = f"shell command '{preview}'"
        elif mcp_details:
            mcp_server = mcp_details.get("server_name")
            action_summary = f"MCP tool '{tool_name}' on server '{mcp_server}'"
        elif subagent_details:
            subagent_name = subagent_details.get("target_subagent")
            action_summary = f"dispatching subagent '{subagent_name}'"
        elif skill_details:
            action_summary = f"skill execution '{skill_details.get('active_skill')}'"
        else:
            action_summary = f"tool '{tool_name}'"

        # 4. Multi-tier decision policy
        requires_interrupt = False
        if is_mutating:
            if is_prod:
                requires_interrupt = True
                rationale = f"Mutating action targeting production environment ({action_summary})"
            elif p_aligned < 0.60:
                requires_interrupt = True
                rationale = (
                    f"Mutating action diverges from user request/goal (alignment prob: {p_aligned:.2f}) on {action_summary}"
                )
            elif (
                score_val >= 1.2
                or p_critical >= 0.35
                or (shell_details and shell_details.get("is_ast_destructive"))
                or (mcp_details and mcp_details.get("destructive_hint"))
            ):
                requires_interrupt = True
                if p_critical >= 0.35 and score_val < 1.2:
                    rationale = f"Elevated Critical blast radius probability ({p_critical:.0%}) on {action_summary}"
                else:
                    rationale = f"Elevated blast radius ({score_val:.2f}) on {action_summary}"
            elif confidence < 0.65:
                requires_interrupt = True
                rationale = f"Low classifier confidence ({confidence:.2f}); escalating to human confirmation for {action_summary}"
            elif action_type == "subagent_dispatch" and (
                score_val >= 1.0 or (subagent_details and subagent_details.get("has_execution_privilege") and p_mutating >= 0.50)
            ):
                requires_interrupt = True
                rationale = f"Subagent delegation with mutating/execution capabilities ({action_summary})"
            elif is_deep_agent and score_val >= 1.0:
                requires_interrupt = True
                rationale = f"Autonomous deep agent '{caller_agent}' attempting state modification ({action_summary})"
            else:
                rationale = (
                    f"Autonomous execution permitted in {active_environment} "
                    f"(risk: {score_val:.2f}, prob: {p_mutating:.2f}) for {action_summary}"
                )
        elif confidence < 0.50 and score_val >= 0.8:
            requires_interrupt = True
            rationale = f"Ambiguous risk profile with low confidence ({confidence:.2f}) for {action_summary}"
        else:
            rationale = f"Safe non-mutating operation (risk: {score_val:.2f}, prob: {p_mutating:.2f}) for {action_summary}"

        logger.info(
            "Jev verdict for %s [%s] (req_id=%s in_tok=%s): mutating=%s (p=%.2f) blast_radius=%.2f (p_crit=%.2f, conf=%.2f) risk=%d interrupt=%s latency=%dms rationale=%s",
            tool_name,
            action_type,
            req_id,
            input_tokens,
            is_mutating,
            p_mutating,
            score_val,
            p_critical,
            confidence,
            risk_level,
            requires_interrupt,
            latency_ms,
            rationale,
        )

        return JevToolVerdict(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            is_mutating=is_mutating,
            mutating_probability=p_mutating,
            blast_radius=score_val,
            risk_level=risk_level,
            requires_human_interrupt=requires_interrupt,
            rationale=rationale,
            latency_ms=latency_ms,
            action_type=action_type,
            caller_agent=caller_agent,
            intent_alignment_probability=p_aligned,
            confidence=confidence,
            critical_probability=p_critical,
            request_id=req_id if req_id else None,
            input_tokens=int(input_tokens) if input_tokens is not None else None,
            output_tokens=int(output_tokens) if output_tokens is not None else None,
            subagent_name=subagent_name,
            mcp_server=mcp_server,
        )

    async def evaluate_batch(
        self,
        calls: Sequence[ToolCall],
        tools: Mapping[str, BaseTool] | None = None,
        active_environment: str = "production",
        worktree_root: str = ".",
        *,
        user_prompt: str = "",
        user_goal: str = "",
        caller_agent: str = "main",
        is_deep_agent: bool = False,
        active_skill: str | None = None,
    ) -> list[JevToolVerdict]:
        """Evaluate a batch of tool calls concurrently in parallel with rich context."""
        tools_map = tools or {}
        tasks = [
            self.evaluate_call(
                tool_call_id=str(call.get("id", "")),
                tool_name=call.get("name", ""),
                tool_args=dict(call.get("args") or {}),
                active_environment=active_environment,
                worktree_root=worktree_root,
                tool=tools_map.get(call.get("name", "")),
                user_prompt=user_prompt,
                user_goal=user_goal,
                caller_agent=caller_agent,
                is_deep_agent=is_deep_agent,
                active_skill=active_skill,
            )
            for call in calls
        ]
        return await asyncio.gather(*tasks)


__all__ = ["JevSecurityClassifier", "JevToolVerdict"]
