"""``/trace`` — Open active thread trace in LangSmith."""

from __future__ import annotations

import asyncio
from opscloud.utils.logger import get_logger
import webbrowser

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel
from opscloud.config.langsmith import (
    LangSmithApiError,
    LangSmithImportError,
    LangSmithLookupTimeoutError,
    LangSmithProjectNotFoundError,
    _assemble_langsmith_thread_url,
    fetch_langsmith_project_url_or_raise,
    get_langsmith_project_name,
)

logger = get_logger(__name__)


class TraceHandler(BaseCommandHandler):
    """Open active thread trace in LangSmith browser UI."""

    @property
    def name(self) -> str:
        return "/trace"

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.POWER

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.LOW_RISK

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.SIDE_EFFECT_FREE

    async def execute(self, ctx: CommandContext) -> CommandResult:
        # Resolve active thread ID
        thread_id = None
        if ctx.app is not None:
            thread_id = getattr(ctx.app, "_agent_thread_id", None) or getattr(
                ctx.app, "_resume_thread", None
            )
        if not thread_id and ctx.session is not None:
            thread_id = getattr(ctx.session, "thread_id", None)
        if not thread_id:
            thread_id = "default"

        # Resolve LangSmith project name
        try:
            project_name = await asyncio.to_thread(get_langsmith_project_name)
        except Exception as exc:
            logger.exception("Failed to resolve LangSmith project name: %s", exc)
            return CommandResult(success=False, message="Failed to resolve LangSmith project name.")

        if not project_name:
            return CommandResult(
                success=False,
                message="LangSmith tracing is not configured. Run `/auth` and select LangSmith to enable tracing.",
            )

        # Resolve project URL from LangSmith
        try:
            project_url = await asyncio.to_thread(
                fetch_langsmith_project_url_or_raise, project_name
            )
        except LangSmithImportError:
            return CommandResult(
                success=False,
                message="The `langsmith` package is not installed. "
                "Install it with `uv add langsmith` or `pip install langsmith` to enable `/trace`.",
            )
        except LangSmithLookupTimeoutError:
            return CommandResult(
                success=False,
                message="Could not reach LangSmith to resolve the thread URL. "
                "Check your network connection and try again.",
            )
        except LangSmithProjectNotFoundError:
            return CommandResult(
                success=False,
                message=f"No traces have been recorded in LangSmith project '{project_name}' yet. "
                "The project is created automatically the first time a run is traced — "
                "try `/trace` again after your first message.",
            )
        except LangSmithApiError as exc:
            return CommandResult(
                success=False,
                message=f"LangSmith rejected the project lookup: {exc}. "
                "Verify `LANGSMITH_API_KEY` and the project name are correct.",
            )
        except Exception as exc:
            logger.exception("Failed to fetch LangSmith project URL: %s", exc)
            return CommandResult(success=False, message="Failed to resolve LangSmith thread URL.")

        url = _assemble_langsmith_thread_url(project_url, thread_id)

        def _open_browser() -> None:
            try:
                webbrowser.open(url)
            except Exception:
                logger.debug("Could not open browser for URL: %s", url, exc_info=True)

        await asyncio.to_thread(_open_browser)

        msg = (
            f"Opening tracing project **{project_name}** in default browser:\n\n"
            f"[{url}]({url})\n\n"
            "_(Note: If you haven't sent a message in this thread yet, the trace view will be empty until your first prompt runs.)_"
        )
        return CommandResult(success=True, message=msg)
