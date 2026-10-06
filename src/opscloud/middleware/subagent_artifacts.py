"""Middleware to inject artifact and reporting guidelines for subagents."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import SystemMessage

from opscloud.middleware.registry import register_middleware
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

ARTIFACT_GUIDELINES = """
## Artifacts & Reporting Mechanics

You are equipped with the ability to generate "Artifacts". An Artifact is a persistent, well-formatted Markdown document used to present structured information (e.g., an AWS security audit, an architecture review, or tabular pricing data).

CRITICAL RULE:
- Do NOT create excessive markdown summary files after completing work.
- Focus on the work itself, not documenting what you did.
- Only create documentation or reports when EXPLICITLY requested by the user. For all other work, use a standard text response.

When generating a requested report:
1. Use your file writing tools to save the report to the local file system. 
2. Always save reports in the workspace with a descriptive filename (e.g., `s3_audit_report.md`).
3. Format the content using standard GitHub Flavored Markdown (Mermaid diagrams, tables).
4. DO NOT re-summarize the entire report in your text response. Simply provide the file path and highlight the top 2 critical findings.

## Context Management & Summarization
- You are equipped with automatic context compaction and the `compact_conversation` tool.
- If your delegated task requires extensive exploration, executing many CLI/API commands, or reading large files, older turns are automatically summarized into concise milestones and offloaded to `/conversation_history/` on the workspace backend to preserve your context window.
- When delivering your final result back to the orchestrator, provide a crisp, direct summary of your actions, discovered findings, and modified resources so the parent agent has immediate clarity.
"""

@register_middleware(name="subagent_artifacts")
class SubagentArtifactsMiddleware(AgentMiddleware[Any, Any]):
    """Middleware that injects Artifact reporting rules into the subagent's system prompt."""

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse | ExtendedModelResponse:
        
        # Inject the guidelines into the existing system message
        if request.system_message is not None:
            curr_content = request.system_message.content or ""
            if isinstance(curr_content, str):
                new_content = curr_content + "\n" + ARTIFACT_GUIDELINES
            elif isinstance(curr_content, list):
                new_content = list(curr_content) + [ARTIFACT_GUIDELINES]
            else:
                new_content = ARTIFACT_GUIDELINES
            
            request = request.override(system_message=SystemMessage(content=new_content))
            
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | ExtendedModelResponse:
        
        if request.system_message is not None:
            curr_content = request.system_message.content or ""
            if isinstance(curr_content, str):
                new_content = curr_content + "\n" + ARTIFACT_GUIDELINES
            elif isinstance(curr_content, list):
                new_content = list(curr_content) + [ARTIFACT_GUIDELINES]
            else:
                new_content = ARTIFACT_GUIDELINES
            
            request = request.override(system_message=SystemMessage(content=new_content))
            
        return await handler(request)
