"""Middleware that manages subagent metadata, delegation rules, and system-prompt injection."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import SystemMessage

from opscloud.middleware.registry import register_middleware
from opscloud.subagents.types import SubagentMetadata
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    from langgraph.runtime import Runtime


@register_middleware(name="subagents")
class SubagentsMiddleware(AgentMiddleware):
    """Middleware that manages subagent metadata and system-prompt injection.

    Features:
    - Dynamic registration and discovery of subagents.
    - Explicit bifurcation between Built-in subagents (direct `task` tool)
      and Plugin/Extension subagents (Code Interpreter `js_eval` primitive).
    - Preserves subagent registry metadata in state for tools.
    """

    def __init__(
        self,
        subagent_metas: Sequence[SubagentMetadata | Mapping[str, Any]] | None = None,
        *,
        planning_mode: bool = False,
    ) -> None:
        super().__init__()
        self.planning_mode = planning_mode
        self._registry: dict[str, SubagentMetadata] = {}
        if subagent_metas:
            for meta in subagent_metas:
                name = meta.get("name", "")
                if name:
                    self._registry[name] = dict(meta)  # type: ignore[arg-type]

    @property
    def subagent_names(self) -> list[str]:
        """Return sorted list of registered subagent names."""
        return sorted(self._registry.keys())

    def get_subagent(self, name: str) -> SubagentMetadata | None:
        """Look up a subagent by exact name."""
        return self._registry.get(name)

    def register_subagent(self, meta: SubagentMetadata | Mapping[str, Any]) -> None:
        """Add or replace a subagent registration at runtime."""
        name = meta.get("name", "")
        if not name:
            logger.warning("[SubagentsMiddleware] Cannot register subagent without a name")
            return
        self._registry[name] = dict(meta)  # type: ignore[arg-type]
        logger.debug("[SubagentsMiddleware] Registered subagent: %s", name)

    def list_subagents(self) -> list[SubagentMetadata]:
        """Return all registered subagent metadata dicts."""
        return list(self._registry.values())

    def _is_plugin_subagent(self, meta: SubagentMetadata) -> bool:
        """Determine whether a subagent is plugin-provided based on metadata."""
        name = meta.get("name", "")
        source = meta.get("source", "")
        return (
            "plugin" in source.lower()
            or "@" in name
            or bool(meta.get("is_plugin"))
        )

    def _build_prompt_block(self) -> str:
        """Build the system-prompt fragment dynamically listing available subagents."""
        if not self._registry:
            return ""

        if self.planning_mode:
            lines: list[str] = ["\n\n## Subagent Delegation & Operational Capabilities\n"]
            lines.append(
                "OpsCloud operates with specialized autonomous subagents to execute and verify tasks. "
                "You do NOT execute tasks or run subagents yourself. "
                "Use this context solely to understand the operational capabilities and verification boundaries available when drafting acceptance criteria:\n"
            )
            for name in sorted(self._registry):
                meta = self._registry[name]
                desc = meta.get("description", "No description provided.")
                lines.append(f"- **`{name}`**: {desc}")
            return "\n".join(lines)

        builtin_agents: list[SubagentMetadata] = []
        plugin_agents: list[SubagentMetadata] = []

        for name in sorted(self._registry):
            meta = self._registry[name]
            if self._is_plugin_subagent(meta):
                plugin_agents.append(meta)
            else:
                builtin_agents.append(meta)

        lines: list[str] = ["\n\n## Subagent Delegation & Orchestration Architecture\n"]
        lines.append(
            "You have access to specialized subagents for task delegation. "
            "Delegation channels are structured based on subagent origin:"
        )

        # 1. Built-in Subagents
        if builtin_agents:
            lines.append("\n### 1. Built-in Subagents (Direct `task` Tool)")
            lines.append("Use the native `task` tool call directly from chat for these core subagents:\n")
            for meta in builtin_agents:
                name = meta.get("name", "")
                desc = meta.get("description", "No description provided.")
                lines.append(f"- **`{name}`**: {desc}")
            lines.append("\n*Invocation Format:*")
            lines.append('`task(description="...", subagent_type="<subagent_name>")`')

        # 2. Plugin & Extension Subagents
        if plugin_agents:
            lines.append("\n---\n")
            lines.append("### 2. Plugin & Extension Subagents (Code Interpreter `js_eval`)")
            lines.append(
                "Plugin-provided subagents MUST be triggered through the `js_eval` Code Interpreter "
                "tool using the embedded `task()` JavaScript primitive. This enables programmatic fan-out, "
                "batching, structured JSON schema verification, and memory isolation.\n"
            )
            for meta in plugin_agents:
                name = meta.get("name", "")
                desc = meta.get("description", "No description provided.")
                source = meta.get("source", "")
                skills = meta.get("skills")

                lines.append(f"- **`{name}`**: {desc}")
                if source:
                    lines.append(f"  - Source: `{source}`")
                if skills:
                    skill_str = ", ".join(f"`{s}`" for s in skills)
                    lines.append(f"  - Skills: {skill_str}")

            lines.append("\n*Invocation Format (via `js_eval`):*")
            lines.append("```javascript")
            lines.append("const result = await task({")
            lines.append('  description: "...",')
            lines.append('  subagentType: "<subagent_name>",')
            lines.append("  responseSchema: { ... }")
            lines.append("});")
            lines.append("return result;")
            lines.append("```")

        lines.append("\n---\n")
        lines.append("### Automatic Routing Rules\n")
        lines.append("1. **Direct Built-in Call**: For Built-in subagents, invoke `task(...)` directly.")
        lines.append("2. **Plugin Parallelization**: For multiple resources or files, prefer `js_eval` with `Promise.all`.\n")

        lines.append("\n---\n")
        lines.append("### Subagent Deliverable Presentation Protocol\n")
        lines.append(
            "- Subagents return authoritative, production-grade technical deliverables (detailed Markdown tables, "
            "spend/inventory breakdowns, audit classifications, and exact remediation CLI commands).\n"
            "- When a subagent completes, the orchestrator MUST relay and present this deliverable in its full technical richness to the user.\n"
            "- **DO NOT compress or suppress subagent reports into brief synopsis bullets.** Retain all structured tables, regional breakdowns, resource inventories, and copy-pasteable CLI commands (`aws ...`, `kubectl ...`, `terraform ...`) in the final response."
        )

        return "\n".join(lines)

    def before_agent(
        self,
        state: AgentState,
        runtime: Runtime,
    ) -> dict[str, Any] | None:
        """Inject subagent registry metadata into state for reference by tools."""
        if not self._registry:
            return None

        return {
            "_subagent_registry": {
                name: {
                    "name": meta.get("name", ""),
                    "description": meta.get("description", ""),
                    "source": meta.get("source", ""),
                    "skills": meta.get("skills"),
                    "tools": meta.get("tools"),
                    "is_plugin": self._is_plugin_subagent(meta),
                }
                for name, meta in self._registry.items()
            },
        }

    async def abefore_agent(
        self,
        state: AgentState,
        runtime: Runtime,
    ) -> dict[str, Any] | None:
        return self.before_agent(state, runtime)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse | ExtendedModelResponse:
        block = self._build_prompt_block()
        if block and request.system_message is not None:
            curr_content = request.system_message.content or ""
            if isinstance(curr_content, str):
                new_content = curr_content + block
            elif isinstance(curr_content, list):
                new_content = list(curr_content) + [block]
            else:
                new_content = block
            request = request.override(system_message=SystemMessage(content=new_content))
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | ExtendedModelResponse:
        block = self._build_prompt_block()
        if block and request.system_message is not None:
            curr_content = request.system_message.content or ""
            if isinstance(curr_content, str):
                new_content = curr_content + block
            elif isinstance(curr_content, list):
                new_content = list(curr_content) + [block]
            else:
                new_content = block
            request = request.override(system_message=SystemMessage(content=new_content))
        return await handler(request)


__all__ = ["SubagentsMiddleware"]
