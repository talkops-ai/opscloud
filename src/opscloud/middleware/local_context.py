"""Middleware for injecting local environment, cloud context, and workspace status into system prompt.

Detects OS, git state, project structure, package managers, runtimes,
cloud toolings (AWS, kubectl, helm, terraform, docker), and directory layout
by running a bash script via the backend (local shell or remote sandbox).
Because the script executes inside the backend, the same detection logic
works regardless of where the agent runs.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import json
import logging
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    NotRequired,
    Protocol,
    cast,
    runtime_checkable,
)

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
    ModelResponse,
    PrivateStateAttr,
)
from langchain_core.messages import SystemMessage

from opscloud.config.aws import get_aws_context
from opscloud.middleware.registry import register_middleware
from opscloud.security.unicode_security import sanitize_control_chars
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from deepagents.backends.protocol import ExecuteResponse
    from deepagents.middleware.summarization import SummarizationEvent
    from langgraph.runtime import Runtime


_TOOL_NAME_DISPLAY_LIMIT = 10
"""Maximum number of tool names shown per MCP server in the system prompt."""

_DETECT_SCRIPT_TIMEOUT = 30
"""Timeout in seconds for the environment detection script."""

_MCP_ERROR_DETAIL_LIMIT = 200
"""Max characters of an MCP server error surfaced in the system prompt."""

_TRACING_PROJECT_NAME_LIMIT = 200
"""Max characters of a LangSmith project name surfaced in the system prompt."""


def _sanitize_error_detail(error: str | None) -> str:
    """Make an untrusted error string safe to embed in the system prompt."""
    if not error:
        return "unknown error"
    sanitized = sanitize_control_chars(error, max_length=_MCP_ERROR_DETAIL_LIMIT)
    return sanitized or "unknown error"


def _sanitize_tracing_project_name(project: str) -> str:
    """Make an untrusted LangSmith project name safe for the system prompt."""
    sanitized = sanitize_control_chars(project, max_length=_TRACING_PROJECT_NAME_LIMIT)
    return sanitized or "unknown project"


def _quote_tracing_project_name(project: str) -> str:
    return json.dumps(project, ensure_ascii=False)


def _build_mcp_context(servers: list[Any]) -> str:
    """Format MCP server/tool inventory for the system prompt."""
    if not servers:
        return ""

    total_tools = sum(len(getattr(s, "tools", ())) for s in servers)
    lines = [f"**MCP Servers** ({len(servers)} servers, {total_tools} tools):"]

    for server in servers:
        name = getattr(server, "name", str(server))
        transport = getattr(server, "transport", "stdio")
        tools = getattr(server, "tools", ())
        status = getattr(server, "status", "ok")
        error = getattr(server, "error", None)

        if not tools:
            if status == "error":
                detail = _sanitize_error_detail(error)
                lines.append(
                    f"- **{name}** ({transport}): "
                    f"FAILED TO LOAD — <error>{detail}</error>. "
                    "Treat this integration as temporarily unavailable."
                )
            elif status == "disabled":
                lines.append(f"- **{name}** ({transport}): (disabled by user)")
            else:
                lines.append(f"- **{name}** ({transport}): (no tools registered)")
            continue

        names = [getattr(t, "name", str(t)) for t in tools]
        if len(names) > _TOOL_NAME_DISPLAY_LIMIT:
            shown = ", ".join(names[:_TOOL_NAME_DISPLAY_LIMIT])
            remaining = len(names) - _TOOL_NAME_DISPLAY_LIMIT
            lines.append(f"- **{name}** ({transport}): {shown}, and {remaining} more")
        else:
            lines.append(f"- **{name}** ({transport}): {', '.join(names)}")

    return "\n".join(lines)


def _build_tracing_context(
    agent_project: str | None,
    user_project: str | None,
) -> str:
    """Format LangSmith tracing project names for the system prompt."""
    if not agent_project:
        return ""

    safe_agent_project = _sanitize_tracing_project_name(agent_project)
    quoted_agent_project = _quote_tracing_project_name(safe_agent_project)
    lines = [
        "**LangSmith Tracing**:",
        f"- Agent traces: project {quoted_agent_project}",
    ]
    if user_project:
        safe_user_project = _sanitize_tracing_project_name(user_project)
        if safe_user_project != safe_agent_project:
            quoted_user_project = _quote_tracing_project_name(safe_user_project)
            lines.append(f"- Shell-command traces: project {quoted_user_project}")
    return "\n".join(lines)


def _build_aws_context() -> str:
    """Format active AWS identity and profile for system prompt."""
    try:
        from opscloud.config.cloud_profiles import get_active_aws_profile, get_active_aws_region

        ctx = get_aws_context()
        profile_name = ctx.profile or get_active_aws_profile()
        region_name = ctx.region or get_active_aws_region() or "us-east-1"

        lines = [
            "**AWS Operational Environment**:",
            f"- Active Profile: `{profile_name}`",
            f"- Region: `{region_name}`",
            f"- Account: `{ctx.account_id or 'unknown'}`",
            f"- Identity ARN: `{ctx.arn or 'configured'}`",
            f"- Authentication Status: **{'Authenticated' if ctx.authenticated else 'Active Named Profile'}**",
            f"- Execution Notice: Shell commands (`aws ...`), AWS SDK, and Terraform automatically execute under active AWS profile `{profile_name}` in region `{region_name}`. You do not need to query profiles or environment variables before performing AWS operations.",
        ]
        return "\n".join(lines)
    except Exception as exc:
        logger.debug("Failed retrieving AWS context: %s", exc)
        return ""


@runtime_checkable
class _ExecutableBackend(Protocol):
    def execute(self, command: str, *, timeout: int | None = None) -> Any: ...


@runtime_checkable
class _AsyncExecutableBackend(Protocol):
    async def aexecute(self, command: str, *, timeout: int | None = None) -> Any: ...


# ---------------------------------------------------------------------------
# Modular Detection Script
# ---------------------------------------------------------------------------

def _section_header() -> str:
    return r"""CWD="$(pwd)"
echo "## Local Context"
echo ""
echo "**Current Directory**: \`${CWD}\`"
echo ""

# --- Check git and resolve root ---
IN_GIT=false
ROOT=""
if command -v git >/dev/null 2>&1; then
  GIT_INFO="$(git rev-parse --is-inside-work-tree --show-toplevel 2>/dev/null)"
  GIT_MODE="${GIT_INFO%%$'\n'*}"
  case "$GIT_MODE" in
    true)
      IN_GIT=true
      ROOT="${GIT_INFO#*$'\n'}"
      ;;
    false) IN_GIT=true ;;
  esac
fi"""


def _section_project() -> str:
    return r"""# --- Project ---
PROJ_LANG=""
{ [ -f main.tf ] || [ -f terraform.tf ] || [ -f terraform.tfvars ]; } && PROJ_LANG="terraform"
[ -z "$PROJ_LANG" ] && [ -f terragrunt.hcl ] && PROJ_LANG="terragrunt"
[ -z "$PROJ_LANG" ] && [ -f Chart.yaml ] && PROJ_LANG="helm-chart"
[ -z "$PROJ_LANG" ] && [ -f cdk.json ] && PROJ_LANG="aws-cdk"
[ -z "$PROJ_LANG" ] && { [ -f serverless.yml ] || [ -f serverless.ts ]; } && PROJ_LANG="serverless"
[ -z "$PROJ_LANG" ] && { [ -f pyproject.toml ] || [ -f setup.py ]; } && PROJ_LANG="python"
[ -z "$PROJ_LANG" ] && [ -f go.mod ] && PROJ_LANG="go"
[ -z "$PROJ_LANG" ] && [ -f package.json ] && PROJ_LANG="javascript/typescript"

MONOREPO=false
{ [ -d modules ] || [ -d environments ] || [ -d stacks ] \
  || [ -f pnpm-workspace.yaml ] || [ -d packages ] || [ -d workspaces ]; } && MONOREPO=true

ENVS=""
{ [ -d .venv ] || [ -d venv ]; } && ENVS=".venv"

HAS_PROJECT=false
{ [ -n "$PROJ_LANG" ] || { [ -n "$ROOT" ] && [ "$ROOT" != "$CWD" ]; } \
  || $MONOREPO || [ -n "$ENVS" ]; } && HAS_PROJECT=true

if $HAS_PROJECT; then
  echo "**Project**:"
  [ -n "$PROJ_LANG" ] && echo "- Language / Framework: ${PROJ_LANG}"
  [ -n "$ROOT" ] && [ "$ROOT" != "$CWD" ] && echo "- Project root: \`${ROOT}\`"
  $MONOREPO && echo "- Monorepo: yes"
  [ -n "$ENVS" ] && echo "- Environments: ${ENVS}"
  echo ""
fi"""


def _section_git() -> str:
    return r"""# --- Git ---
if $IN_GIT; then
  BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
  if [ "$BRANCH" = "HEAD" ]; then
    COMMIT="$(git rev-parse --short HEAD 2>/dev/null)"
    GT="**Git**: Detached HEAD at \`${COMMIT}\`"
  else
    GT="**Git**: Current branch \`${BRANCH}\`"
  fi

  MAINS=""
  for b in $(git for-each-ref --format='%(refname:short)' \
      refs/heads/main refs/heads/master 2>/dev/null); do
    case "$b" in
      main) MAINS="${MAINS:+${MAINS}, }\`main\`" ;;
      master) MAINS="${MAINS:+${MAINS}, }\`master\`" ;;
    esac
  done
  [ -n "$MAINS" ] && GT="${GT}, ${MAINS} available"

  DC=$(git status --porcelain 2>/dev/null | awk 'END { print NR }')
  if [ "$DC" -gt 0 ]; then
    if [ "$DC" -eq 1 ]; then GT="${GT}, 1 uncommitted change"
    else GT="${GT}, ${DC} uncommitted changes"
    fi
  fi

  echo "$GT"
  echo ""
fi"""


def build_detect_script() -> str:
    """Assemble workspace detection script (Directory, Project type, Git status)."""
    return "\n\n".join(
        [
            _section_header(),
            _section_project(),
            _section_git(),
        ]
    )


DETECT_CONTEXT_SCRIPT = build_detect_script()


class LocalContextState(AgentState):
    """State schema for local context caching."""

    _local_context: Annotated[NotRequired[str | None], PrivateStateAttr]
    _local_context_refreshed_at_cutoff: Annotated[NotRequired[int | None], PrivateStateAttr]


@register_middleware(name="local_context")
class LocalContextMiddleware(AgentMiddleware):
    """Inject local context, git state, and cloud environment into the system prompt."""

    state_schema = LocalContextState

    def __init__(
        self,
        backend: _ExecutableBackend | _AsyncExecutableBackend | None = None,
        *,
        working_dir: Any = None,
        tracing_project: str | None = None,
        user_tracing_project: str | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self.backend = backend
        self.working_dir = working_dir
        self.tracing_project = tracing_project
        self.user_tracing_project = user_tracing_project
        self.mcp_servers = list(mcp_servers or [])

    @property
    def _static_context(self) -> str:
        parts = []
        aws_ctx = _build_aws_context()
        if aws_ctx:
            parts.append(aws_ctx)
        mcp_ctx = _build_mcp_context(self.mcp_servers)
        if mcp_ctx:
            parts.append(mcp_ctx)
        tracing_ctx = _build_tracing_context(self.tracing_project, self.user_tracing_project)
        if tracing_ctx:
            parts.append(tracing_ctx)
        return "\n\n".join(parts)

    @staticmethod
    def _handle_detect_result(result: Any) -> str | None:
        if result is None:
            return None
        output = getattr(result, "output", "") or ""
        text = output.strip() if isinstance(output, str) else ""
        return text or None

    def _run_detect_script(self) -> str | None:
        backend = self.backend
        if backend is None:
            return None
        execute_fn = getattr(backend, "execute", None)
        if callable(execute_fn):
            try:
                res = execute_fn(DETECT_CONTEXT_SCRIPT, timeout=_DETECT_SCRIPT_TIMEOUT)
                return self._handle_detect_result(res)
            except Exception as e:
                logger.debug("Local context detection sync execution failed: %s", e)
                return None
        return None

    async def _arun_detect_script(self) -> str | None:
        backend = self.backend
        if backend is None:
            return None
        aexecute_fn = getattr(backend, "aexecute", None)
        if callable(aexecute_fn):
            try:
                res = aexecute_fn(DETECT_CONTEXT_SCRIPT, timeout=_DETECT_SCRIPT_TIMEOUT)
                if inspect.isawaitable(res):
                    res = await res
                return self._handle_detect_result(res)
            except Exception as e:
                logger.debug("Local context detection async execution failed: %s", e)
                return None
        try:
            return await asyncio.to_thread(self._run_detect_script)
        except Exception as e:
            logger.debug("Local context detection thread fallback failed: %s", e)
            return None

    def before_agent(
        self,
        state: AgentState,
        runtime: Runtime,
    ) -> dict[str, Any] | None:
        raw_event = state.get("_summarization_event")
        if raw_event is not None:
            event = cast("SummarizationEvent", raw_event)
            cutoff = event.get("cutoff_index")
            refreshed_cutoff = state.get("_local_context_refreshed_at_cutoff")
            if cutoff != refreshed_cutoff:
                output = self._run_detect_script()
                if output:
                    return {
                        "_local_context": output,
                        "_local_context_refreshed_at_cutoff": cutoff,
                    }
                return {"_local_context_refreshed_at_cutoff": cutoff}

        if state.get("_local_context"):
            return None

        output = self._run_detect_script()
        if output:
            return {"_local_context": output}
        return None

    async def abefore_agent(
        self,
        state: AgentState,
        runtime: Runtime,
    ) -> dict[str, Any] | None:
        raw_event = state.get("_summarization_event")
        if raw_event is not None:
            event = cast("SummarizationEvent", raw_event)
            cutoff = event.get("cutoff_index")
            refreshed_cutoff = state.get("_local_context_refreshed_at_cutoff")
            if cutoff != refreshed_cutoff:
                output = await self._arun_detect_script()
                if output:
                    return {
                        "_local_context": output,
                        "_local_context_refreshed_at_cutoff": cutoff,
                    }
                return {"_local_context_refreshed_at_cutoff": cutoff}

        if state.get("_local_context"):
            return None

        output = await self._arun_detect_script()
        if output:
            return {"_local_context": output}
        return None

    def _get_modified_request(self, request: ModelRequest) -> ModelRequest | None:
        state = getattr(request, "state", {}) or {}
        local_context = state.get("_local_context") if isinstance(state, dict) else getattr(state, "_local_context", None)
        system_prompt = request.system_prompt or ""

        parts = [p for p in (system_prompt, local_context, self._static_context) if p]
        if not parts or parts == [system_prompt]:
            return None

        return request.override(
            system_message=SystemMessage(content="\n\n".join(parts))
        )

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        modified_request = self._get_modified_request(request)
        return handler(modified_request or request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        modified_request = self._get_modified_request(request)
        return await handler(modified_request or request)


__all__ = [
    "DETECT_CONTEXT_SCRIPT",
    "LocalContextMiddleware",
    "LocalContextState",
    "build_detect_script",
]
