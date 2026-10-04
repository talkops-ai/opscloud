"""MCP Semantic Profiler — registration-time tool risk profiling and security classification.

Profiles MCP tools during server connection / registration, caching 4-tier risk metadata
(Tier 1: Read-Only, Tier 2: Low-Impact, Tier 3: Mutating, Tier 4: Destructive) to enable
sub-millisecond runtime safety gating without per-turn LLM latency.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

PROFILER_SYSTEM_PROMPT = """You are an expert Cloud & Infrastructure Security Auditor and Zero-Trust Architect.
Your task is to analyze a newly connected Model Context Protocol (MCP) tool and classify its operational risk profile into one of 4 Tiers.

You must ignore benign-sounding verbs in the tool name (e.g., "sync", "process", "audit", "remediate") if the description or JSON schema implies underlying mutation or destruction.

Tier Definitions:
- TIER 1 (Read-Only): Strictly idempotent inspection. No state is altered. (e.g. get, list, describe, query, status, logs).
- TIER 2 (Low-Impact): Reversible or transient mutations. (e.g. dry-runs, scratch files, non-critical labels/annotations).
- TIER 3 (Mutating): Modifies live infrastructure state. (e.g. apply, install, scale, update, rollout, sync, patch).
- TIER 4 (Destructive): Irreversible deletion, eviction, draining, database drops, termination, or credential manipulation. (e.g. delete, destroy, terminate, uninstall, drain, drop, prune, zap, wipe).

Output Constraints:
You must respond strictly with valid JSON matching the ToolSafetyProfile schema.
"""


class ToolSafetyProfile(BaseModel):
    """Immutable safety profile for an MCP tool."""

    model_config = ConfigDict(extra="ignore")

    tool_name: str
    inferred_tier: int = Field(default=3, ge=1, le=4, description="The operational risk tier (1 to 4).")
    read_only_hint: bool = Field(default=False, description="True if strictly Tier 1, False otherwise.")
    destructive_hint: bool = Field(default=False, description="True if Tier 4, False otherwise.")
    sensitive_arguments: list[str] = Field(
        default_factory=list,
        description="Argument names from schema that trigger destructive or elevated behavior (e.g. 'force', 'purge', 'cascade').",
    )
    justification: str = Field(default="", description="Concise security justification for the classification.")


class MCPSemanticProfiler:
    """Manages and caches registration-time security profiles for MCP tools."""

    _instance: MCPSemanticProfiler | None = None

    @classmethod
    def get_instance(cls) -> MCPSemanticProfiler:
        """Retrieve the singleton instance of MCPSemanticProfiler.

        Returns:
            MCPSemanticProfiler: The singleton profiler instance.
        """
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        """Initialize MCPSemanticProfiler with an empty profile registry."""
        # Keyed by f"{server_name}:{tool_name}" and "tool_name"
        self._profiles: dict[str, ToolSafetyProfile] = {}

    def get_profile(self, server_name: str, tool_name: str) -> ToolSafetyProfile | None:
        """Lookup cached profile by scoped or raw tool name."""
        scoped_key = f"{server_name}:{tool_name}" if server_name else tool_name
        return self._profiles.get(scoped_key) or self._profiles.get(tool_name)

    def register_profile(
        self,
        server_name: str,
        tool_name: str,
        profile: ToolSafetyProfile,
    ) -> None:
        """Register a safety profile in memory cache."""
        scoped_key = f"{server_name}:{tool_name}" if server_name else tool_name
        self._profiles[scoped_key] = profile
        self._profiles[tool_name] = profile
        logger.debug(
            "Registered MCP safety profile: %s -> Tier %d (readonly=%s, destructive=%s)",
            scoped_key,
            profile.inferred_tier,
            profile.read_only_hint,
            profile.destructive_hint,
        )

    def heuristic_profile(self, mcp_tool: Any, server_name: str = "") -> ToolSafetyProfile:
        """Derive a zero-trust safety profile from tool annotations and metadata."""
        name = getattr(mcp_tool, "name", str(mcp_tool))
        if ":" in name and (not server_name or name.startswith(f"{server_name}:")):
            original_name = name.split(":", 1)[1]
        else:
            original_name = name

        raw_annotations = getattr(mcp_tool, "annotations", None)
        annotations: dict[str, Any] = {}
        if isinstance(raw_annotations, dict):
            annotations = raw_annotations
        elif raw_annotations is not None:
            dump_fn = getattr(raw_annotations, "model_dump", None)
            if callable(dump_fn):
                dumped = dump_fn(by_alias=True)
                if isinstance(dumped, dict):
                    annotations = dumped
            elif hasattr(raw_annotations, "__dict__"):
                annotations = vars(raw_annotations)

        read_only_hint = bool(annotations.get("readOnlyHint", False))
        destructive_hint = bool(annotations.get("destructiveHint", False))

        desc = getattr(mcp_tool, "description", "") or ""
        lower_name = original_name.lower()
        desc.lower()

        # Tokenize name by underscores, hyphens, and colons
        name_tokens = set(re.findall(r"[a-z0-9]+", lower_name))

        destructive_verbs = {
            "delete",
            "destroy",
            "drop",
            "drain",
            "evict",
            "prune",
            "zap",
            "wipe",
            "uninstall",
            "purge",
            "kill",
            "terminate",
            "erase",
            "format",
            "remove",
            "deregister",
            "disassociate",
            "stop",
            "reboot",
        }

        inspection_verbs = {
            "get",
            "list",
            "describe",
            "view",
            "status",
            "query",
            "read",
            "inspect",
            "search",
            "show",
            "check",
            "fetch",
            "diff",
            "find",
            "lookup",
            "info",
            "cat",
            "tail",
            "watch",
            "scan",
            "metrics",
            "logs",
            "events",
            "ping",
            "test",
            "validate",
            "explain",
            "history",
            "top",
            "version",
            "pricing",
            "cost",
            "estimate",
        }

        # 1. Explicit annotations always take highest precedence
        if destructive_hint:
            return ToolSafetyProfile(
                tool_name=original_name,
                inferred_tier=4,
                read_only_hint=False,
                destructive_hint=True,
                sensitive_arguments=[
                    "force",
                    "purge",
                    "cascade",
                    "grace_period",
                    "instance_id",
                    "bucket",
                    "cluster_name",
                    "stack_name",
                ],
                justification="Explicit destructive annotation (destructiveHint=True).",
            )

        if read_only_hint:
            return ToolSafetyProfile(
                tool_name=original_name,
                inferred_tier=1,
                read_only_hint=True,
                destructive_hint=False,
                sensitive_arguments=[],
                justification="Explicit read-only annotation (readOnlyHint=True).",
            )

        # 2. Heuristics fallback: Tier 4 (destructive verb)
        if any(v in name_tokens for v in destructive_verbs) or any(v in lower_name for v in destructive_verbs):
            return ToolSafetyProfile(
                tool_name=original_name,
                inferred_tier=4,
                read_only_hint=False,
                destructive_hint=True,
                sensitive_arguments=[
                    "force",
                    "purge",
                    "cascade",
                    "grace_period",
                    "instance_id",
                    "bucket",
                    "cluster_name",
                    "stack_name",
                ],
                justification="Destructive verb in tool name.",
            )

        # 3. Heuristics fallback: Tier 1 (inspection verb or prefix)
        if any(v in name_tokens for v in inspection_verbs) or lower_name.startswith(
            (
                "get_",
                "list_",
                "describe_",
                "view_",
                "status_",
                "query_",
                "read_",
                "inspect_",
                "search_",
                "fetch_",
                "show_",
                "check_",
                "find_",
                "lookup_",
                "scan_",
            )
        ):
            return ToolSafetyProfile(
                tool_name=original_name,
                inferred_tier=1,
                read_only_hint=True,
                destructive_hint=False,
                sensitive_arguments=[],
                justification="Standard inspection verb or prefix in tool name.",
            )

        # Tier 2: Dry run or temporary scratch
        if "dry_run" in lower_name or "dryrun" in lower_name or "scratch" in lower_name or "plan" in lower_name:
            return ToolSafetyProfile(
                tool_name=original_name,
                inferred_tier=2,
                read_only_hint=False,
                destructive_hint=False,
                sensitive_arguments=[],
                justification="Low-impact or dry-run operation.",
            )

        # Tier 3: General state mutation (default)
        return ToolSafetyProfile(
            tool_name=original_name,
            inferred_tier=3,
            read_only_hint=False,
            destructive_hint=False,
            sensitive_arguments=[
                "namespace",
                "release",
                "name",
                "instance_id",
                "bucket",
                "cluster_name",
                "stack_name",
                "role_arn",
            ],
            justification="Operational state mutation.",
        )

    def profile_and_cache_tools(
        self,
        server_name: str,
        tools: list[Any],
    ) -> dict[str, ToolSafetyProfile]:
        """Profile a list of MCP tools and populate cache."""
        results: dict[str, ToolSafetyProfile] = {}
        for tool in tools:
            tool_name = getattr(tool, "name", str(tool))
            profile = self.heuristic_profile(tool, server_name=server_name)
            self.register_profile(server_name, tool_name, profile)
            results[tool_name] = profile
        return results
