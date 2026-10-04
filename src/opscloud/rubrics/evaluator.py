"""Grader tools, prompts, and standalone evaluator for rubric verification.

Provides:
- Isolated read-only tools for inspecting both offloaded outputs and repository files.
- Dynamic grader system prompt builder with domain-aware guidance for AWS Cloud & DevOps.
- ``create_rubric_grader_agent`` to instantiate the Rubric Grader as an individual agent.
- ``evaluate_rubric`` for direct, programmatic quality grading.
"""

from __future__ import annotations

import inspect
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Callable, Sequence

from deepagents.middleware import GRADER_SYSTEM_PROMPT
from deepagents.middleware.rubric import (
    RUBRIC_GRADER_MESSAGE_SOURCE,
    CriterionEval,
    CriterionFail,
    CriterionPass,
    GraderResponse,
    GraderVerdict,
)
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool, StructuredTool, tool

from opscloud.middleware._repository_bounds import (
    RepositoryBounds,
)
from opscloud.middleware.reliable_rubric import RubricGraderState
from opscloud.offload import _artifacts_root
from opscloud.utils.logger import get_logger

if TYPE_CHECKING:
    from deepagents.backends.protocol import BackendProtocol
    from langchain.agents.middleware.types import AgentMiddleware
    from langchain_core.language_models import BaseChatModel

logger = get_logger(__name__)

_RUBRIC_GRADER_READ_FILE_PREFIX = "/large_tool_results/"


# ---------------------------------------------------------------------------
# Grader System Prompt
# ---------------------------------------------------------------------------


def _rubric_grader_system_prompt(
    read_file_prefix: str = _RUBRIC_GRADER_READ_FILE_PREFIX,
    repository_root: str | None = None,
    context_tool_names: Sequence[str] | None = None,
    repository_tool_names: Sequence[str] | None = None,
) -> str:
    """Build the comprehensive grader system prompt for AWS Cloud & DevOps verification."""
    prompt = (
        f"{GRADER_SYSTEM_PROMPT}\n\n"
        "# OpsCloud — Independent Rubric Verification Grader\n\n"
        "You are the independent Rubric Grader for OpsCloud. Your sole responsibility is to evaluate "
        "whether the agent's work satisfies the accepted acceptance criteria without self-evaluation bias.\n\n"
        "## Evidence Inspection Rules\n"
        f"1. When the conversation transcript references an offloaded tool output saved under `{read_file_prefix}` "
        "or an artifacts directory, use the `read_file` tool to inspect the referenced evidence before concluding "
        "that a criterion lacks support.\n"
        "2. Only mark a criterion satisfied if direct, verifiable evidence in the transcript, command outputs, or "
        "on-disk files demonstrates it. Never assume or extrapolate completion.\n"
        "3. If any criterion is not satisfied or is partially completed, provide clear, actionable feedback in the "
        "deficiency report explaining exactly what is missing or failed."
    )

    if repository_root and repository_tool_names:
        tools_desc = ", ".join(f"`{t}`" for t in repository_tool_names)
        prompt += (
            f"\n\n## Working Directory Inspection\n"
            f"You have read-only tools ({tools_desc}) scoped to the working directory rooted at `{repository_root}`.\n"
            "The transcript can truncate long command outputs; prefer directly inspecting modified or created files "
            "(e.g. Terraform configs, CloudFormation templates, Dockerfiles, Kubernetes manifests, scripts, or tests) "
            "to verify the criteria on disk. All repository inspection is read-only and confined to the repository root."
        )

    if context_tool_names:
        c_names = ", ".join(f"`{t}`" for t in context_tool_names)
        prompt += (
            f"\n\n## External Context Tools\n"
            f"Read-only external context tools are available: {c_names}. Use them when a criterion requires "
            "verifying live cloud resources or external states."
        )

    return prompt


# Backward-compatible static system prompt
_RUBRIC_GRADER_SYSTEM_PROMPT = _rubric_grader_system_prompt()


# ---------------------------------------------------------------------------
# Path Safety Validation
# ---------------------------------------------------------------------------


def _validate_rubric_grader_read_path(
    file_path: str,
    allowed_prefix: str | None = None,
    repository_root: str | None = None,
) -> str | None:
    """Validate that a requested file path does not escape allowed boundaries."""
    normalized = file_path.replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    if ".." in parts or "~" in parts:
        return "Invalid path: traversal characters forbidden."

    # Check allowed prefix (e.g. /large_tool_results/ or offload directory)
    if allowed_prefix and normalized.startswith(allowed_prefix):
        return None
    if normalized.startswith(_RUBRIC_GRADER_READ_FILE_PREFIX):
        return None

    # Check offload artifact root
    try:
        art_root = _artifacts_root().root.replace("\\", "/")
        if normalized.startswith(art_root):
            return None
    except Exception:
        pass

    # Check repository root
    if repository_root:
        repo_norm = str(Path(repository_root).resolve()).replace("\\", "/")
        try:
            target_norm = str(Path(file_path).resolve()).replace("\\", "/")
            if target_norm.startswith(repo_norm):
                return None
        except Exception:
            pass

    return f"Rubric grader cannot read path outside authorized directories: {file_path}"


# ---------------------------------------------------------------------------
# Grader Tools Creation
# ---------------------------------------------------------------------------


def _create_rubric_grader_tools(
    backend: Any = None,
    *,
    repository_backend: BackendProtocol | None = None,
    repository_root: str | None = None,
    context_tools: Sequence[BaseTool | Callable[..., Any]] = (),
) -> list[BaseTool]:
    """Create isolated read-only tools for rubric grading."""
    if repository_root:
        effective_root = repository_root
    elif repository_backend is not None:
        effective_root = "/"
    else:
        try:
            from opscloud.project_utils import get_server_project_context

            ctx = get_server_project_context()
            effective_root = str(ctx.user_cwd) if ctx is not None else "/"
        except Exception:
            effective_root = "/"
    read_file_prefix = _RUBRIC_GRADER_READ_FILE_PREFIX
    try:
        read_file_prefix = _artifacts_root().root
    except Exception:
        pass

    bounds: RepositoryBounds | None = None
    if repository_backend is not None and repository_root is not None:
        try:
            bounds = RepositoryBounds(repository_backend, root=repository_root)
        except Exception as e:
            logger.warning("Could not initialize RepositoryBounds for grader: %s", e)

    @tool
    def read_file(file_path: str, offset: int = 0, limit: int = 120) -> str:
        """Read an offloaded tool result or a working-directory file.

        Args:
            file_path: Absolute or workspace path to the file.
            offset: Starting line index (0-based).
            limit: Maximum number of lines to return.
        """
        if err := _validate_rubric_grader_read_path(file_path, read_file_prefix, effective_root):
            return err

        try:
            p = Path(file_path).expanduser().resolve()
            if not p.exists():
                return f"File {file_path} not found."
            if not p.is_file():
                return f"Path {file_path} is not a file."
            content = p.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()
            sliced = lines[offset : offset + limit]
            return "\n".join(sliced)
        except Exception as e:
            return f"Error reading file {file_path}: {e}"

    tools: list[BaseTool] = [read_file]

    root_path = Path(effective_root).resolve()

    def _resolve_target(rel_or_abs: str) -> Path:
        p = Path(rel_or_abs)
        if p.is_absolute():
            return p.resolve()
        return (root_path / p).resolve()

    def _is_safe(target_p: Path) -> bool:
        if bounds:
            return bounds.safe_path(str(target_p))
        try:
            target_p.relative_to(root_path)
            return True
        except ValueError:
            return False

    # Add repository inspection tools if repository_backend or local workspace is bound
    @tool
    def ls(path: str = ".") -> str:
        """List files in the working directory (read-only)."""
        target = _resolve_target(path)
        if not _is_safe(target):
            return "Path is outside the repository bounds."
        if not target.exists():
            return f"Directory does not exist: {path}"
        try:
            entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name))
            return "\n".join(f"{'[DIR] ' if e.is_dir() else '[FILE] '}{e.name}" for e in entries[:200])
        except Exception as e:
            return f"Error listing directory: {e}"

    @tool
    def glob(pattern: str, path: str = ".") -> str:
        """Search for files matching pattern in the repository (read-only)."""
        target = _resolve_target(path)
        if not _is_safe(target):
            return "Path is outside the repository bounds."
        if not target.exists():
            return f"Path does not exist: {path}"
        try:
            matches = [str(p.relative_to(target)) for p in list(target.glob(pattern))[:200]]
            return "\n".join(matches) if matches else "No matching files found."
        except Exception as e:
            return f"Error searching glob: {e}"

    @tool
    def grep(pattern: str, path: str = ".") -> str:
        """Search for text pattern in repository files (read-only)."""
        target = _resolve_target(path)
        if not _is_safe(target):
            return "Path is outside the repository bounds."
        if not target.exists():
            return f"Path does not exist: {path}"
        try:
            results: list[str] = []
            for file_path in target.rglob("*"):
                if file_path.is_file() and not file_path.name.startswith("."):
                    try:
                        content = file_path.read_text(encoding="utf-8", errors="ignore")
                        for idx, line in enumerate(content.splitlines(), start=1):
                            if pattern in line:
                                rel = file_path.relative_to(target)
                                results.append(f"{rel}:{idx}: {line.strip()[:160]}")
                                if len(results) >= 100:
                                    break
                    except Exception:
                        continue
                if len(results) >= 100:
                    break
            return "\n".join(results) if results else "Pattern not found."
        except Exception as e:
            return f"Error running grep: {e}"

    tools.extend([ls, glob, grep])

    # Append any valid context tools
    for candidate in context_tools:
        if isinstance(candidate, BaseTool):
            tools.append(candidate)
        elif inspect.iscoroutinefunction(candidate):
            tools.append(StructuredTool.from_function(coroutine=candidate))
        elif callable(candidate):
            tools.append(StructuredTool.from_function(func=candidate))

    return tools


# ---------------------------------------------------------------------------
# Standalone Rubric Grader Agent Factory & Evaluator
# ---------------------------------------------------------------------------


def create_rubric_grader_agent(
    *,
    model: str | BaseChatModel | None = None,
    tools: Sequence[BaseTool] | None = None,
    middleware: Sequence[AgentMiddleware[Any, Any]] | None = None,
    context_schema: type[Any] | None = None,
    system_prompt: str | None = None,
) -> Any:
    """Create an isolated Rubric Grader Agent as an individual agent graph."""
    from deepagents._models import resolve_model
    from langchain.agents import create_agent

    from opscloud.model.factory import create_model as opscloud_create_model

    if model is None:
        model_res = opscloud_create_model()
        chat_model = model_res.model
    elif isinstance(model, str):
        chat_model = resolve_model(model)
    else:
        chat_model = model

    grader_tools = list(tools) if tools is not None else _create_rubric_grader_tools()
    prompt = system_prompt or _RUBRIC_GRADER_SYSTEM_PROMPT

    return create_agent(
        model=chat_model,
        system_prompt=prompt,
        tools=grader_tools,
        middleware=list(middleware or ()),
        name=RUBRIC_GRADER_MESSAGE_SOURCE,
        response_format=GraderResponse,
        state_schema=RubricGraderState,
        context_schema=context_schema,
    )


def evaluate_rubric(
    criteria: str,
    evidence: str | Sequence[Any],
    *,
    model: str | BaseChatModel | None = None,
    repository_root: str | None = None,
    use_jev: bool | None = None,
) -> GraderResponse:
    """Directly evaluate criteria against evidence using the Rubric Grader Agent or Jev."""
    from langchain_core.messages import BaseMessage

    if use_jev is True:
        from opscloud.rubrics.jev_compiler import JevCriteriaCompiler
        from opscloud.rubrics.jev_grader import JevHybridRubricGrader

        jev_grader = JevHybridRubricGrader()
        if jev_grader.is_available():
            compiled = JevCriteriaCompiler.compile("Direct Evaluation", criteria)
            return jev_grader.grade(
                compiled,
                evidence=evidence,
                fallback_llm_fn=lambda failing: evaluate_rubric(
                    criteria, evidence, model=model, repository_root=repository_root, use_jev=False
                ),
            )

    tools = _create_rubric_grader_tools(repository_root=repository_root)
    grader = create_rubric_grader_agent(model=model, tools=tools)

    if isinstance(evidence, str):
        content = f"<criteria>\n{criteria}\n</criteria>\n\n<transcript>\n{evidence}\n</transcript>"
    elif isinstance(evidence, Sequence):
        rendered = []
        for msg in evidence:
            if isinstance(msg, BaseMessage):
                rendered.append(f"{msg.type}: {msg.content}")
            else:
                rendered.append(str(msg))
        joined = "\n\n".join(rendered)
        content = f"<criteria>\n{criteria}\n</criteria>\n\n<transcript>\n{joined}\n</transcript>"
    else:
        content = f"<criteria>\n{criteria}\n</criteria>\n\n<evidence>\n{evidence}\n</evidence>"

    input_payload = {
        "messages": [HumanMessage(content=content)],
        "rubric_grading_operation_id": "direct_eval",
    }
    result = grader.invoke(input_payload)
    if isinstance(result, GraderResponse):
        return result
    if isinstance(result, dict) and "structured_response" in result:
        structured = result["structured_response"]
        if isinstance(structured, GraderResponse):
            return structured
        if isinstance(structured, dict):
            return GraderResponse.model_validate(structured)

    verdict: GraderVerdict = "satisfied" if "satisfied" in str(result).lower() else "needs_revision"
    explanation = str(result)
    criteria_evals: list[CriterionEval]
    if verdict == "satisfied":
        criteria_evals = [CriterionPass(name="criteria_evaluation", passed=True)]
    else:
        criteria_evals = [CriterionFail(name="criteria_evaluation", passed=False, gap=explanation)]

    return GraderResponse(
        result=verdict,
        explanation=explanation,
        criteria=criteria_evals,
    )
