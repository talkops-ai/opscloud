"""CLI conversation compaction middleware for context window optimization."""

from __future__ import annotations

import asyncio
from functools import partial
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
from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolArg, StructuredTool
from langgraph.types import Command

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain_core.language_models import BaseChatModel
    from langgraph.prebuilt.tool_node import ToolCallRequest

import contextlib

from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

COMPACTION_FAILURE_PREFIX = "Compaction failed"
_OFFLOAD_SEED_ID_PREFIX = "offload-seed-"


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


class _ArchiveReadGuard:
    """Context manager preventing concurrent reads during archive compaction."""

    def __init__(self, backend: BackendProtocol) -> None:
        """Initialize _ArchiveReadGuard wrapping the given storage backend.

        Args:
            backend: Storage backend providing file operations.
        """
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

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Download multiple files synchronously while tracking read failure states.

        Args:
            paths: List of file paths to download.

        Returns:
            List of FileDownloadResponse objects.
        """
        try:
            responses = self._backend.download_files(paths)
        except Exception:
            self._read_failed = True
            raise
        return self._record_response_errors(responses)

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Download multiple files asynchronously while tracking read failure states.

        Args:
            paths: List of file paths to download.

        Returns:
            List of FileDownloadResponse objects.
        """
        try:
            responses = await self._backend.adownload_files(paths)
        except Exception:
            self._read_failed = True
            raise
        return self._record_response_errors(responses)

    def write(self, file_path: str, content: str) -> WriteResult:
        """Write content synchronously only if preceding archive reads succeeded.

        Args:
            file_path: Destination file path.
            content: File content string.

        Returns:
            WriteResult object.
        """
        self._ensure_read_succeeded()
        return self._backend.write(file_path, content)

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        """Write content asynchronously only if preceding archive reads succeeded.

        Args:
            file_path: Destination file path.
            content: File content string.

        Returns:
            WriteResult object.
        """
        self._ensure_read_succeeded()
        return await self._backend.awrite(file_path, content)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """Edit file content synchronously only if preceding archive reads succeeded.

        Args:
            file_path: Target file path.
            old_string: Target substring to replace.
            new_string: Replacement substring.
            replace_all: Whether to replace all occurrences.

        Returns:
            EditResult object.
        """
        self._ensure_read_succeeded()
        return self._backend.edit(file_path, old_string, new_string, replace_all=replace_all)

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """Edit file content asynchronously only if preceding archive reads succeeded.

        Args:
            file_path: Target file path.
            old_string: Target substring to replace.
            new_string: Replacement substring.
            replace_all: Whether to replace all occurrences.

        Returns:
            EditResult object.
        """
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


class CLICompactionMiddleware(SummarizationToolMiddleware):
    """Add explicit forced compaction and runtime model selection for OpsCloud."""

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
        """Wrap synchronous tool call and guard against unauthorized execution during /offload.

        Args:
            request: Tool execution request.
            handler: Synchronous tool handler.

        Returns:
            ToolMessage error or handler result.
        """
        if (rejection := self._offload_rejection(request)) is not None:
            return rejection
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        """Wrap asynchronous tool call and guard against unauthorized execution during /offload.

        Args:
            request: Tool execution request.
            handler: Asynchronous tool handler.

        Returns:
            ToolMessage error or handler result.
        """
        if (rejection := self._offload_rejection(request)) is not None:
            return rejection
        return await handler(request)

    def wrap_model_call(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Run compact-tool prompt injection and delegate to automatic summarization."""
        call_model = partial(super().wrap_model_call, handler=handler)
        runtime = getattr(request, "runtime", None)
        summarization = self._summarization_for_runtime(runtime) if runtime is not None else self._summarization
        return cast(ModelResponse, summarization.wrap_model_call(request, call_model))

    async def awrap_model_call(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Run compact-tool prompt injection and delegate to automatic summarization (async)."""
        call_model = partial(super().awrap_model_call, handler=handler)
        runtime = getattr(request, "runtime", None)
        if runtime is not None:
            summarization = await asyncio.to_thread(self._summarization_for_runtime, runtime)
        else:
            summarization = self._summarization
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
        return create_summarization_middleware(model, backend)

    def _run_forced_compact(self, runtime: ToolRuntime[Any, Any]) -> Command[Any]:
        tool_call_id = runtime.tool_call_id or ""
        try:
            summarization = self._summarization_for_runtime(runtime)
            messages = runtime.state.get("messages", [])
            event = runtime.state.get("_summarization_event")
            effective = summarization._apply_event_to_messages(messages, event)
            effective = _without_offload_seed(effective, tool_call_id)
            cutoff = summarization._determine_cutoff_index(effective)
            if cutoff == 0:
                return self._nothing_to_compact(tool_call_id)

            session_id = _resolve_session_id(summarization, runtime.state)
            to_summarize, _ = summarization._partition_messages(effective, cutoff)
            summary = summarization._create_summary(to_summarize)
            backend = self._guarded_backend(runtime)
            file_path = _offload_to_backend_compat(summarization, backend, to_summarize, session_id)
            return _build_compact_result_compat(
                self, runtime, to_summarize, summary, file_path, event, cutoff, session_id
            )
        except Exception as exc:
            logger.exception("forced compact_conversation failed")
            return self._forced_compact_error(tool_call_id, exc)

    async def _arun_forced_compact(self, runtime: ToolRuntime[Any, Any]) -> Command[Any]:
        tool_call_id = runtime.tool_call_id or ""
        try:
            summarization = await asyncio.to_thread(self._summarization_for_runtime, runtime)
            messages = runtime.state.get("messages", [])
            event = runtime.state.get("_summarization_event")
            effective = summarization._apply_event_to_messages(messages, event)
            effective = _without_offload_seed(effective, tool_call_id)
            cutoff = summarization._determine_cutoff_index(effective)
            if cutoff == 0:
                return self._nothing_to_compact(tool_call_id)

            session_id = _resolve_session_id(summarization, runtime.state)
            to_summarize, _ = summarization._partition_messages(effective, cutoff)
            summary = await summarization._acreate_summary(to_summarize)
            backend = self._guarded_backend(runtime)
            file_path = await _aoffload_to_backend_compat(summarization, backend, to_summarize, session_id)
            return _build_compact_result_compat(
                self, runtime, to_summarize, summary, file_path, event, cutoff, session_id
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
    return CLICompactionMiddleware(
        sdk_middleware._summarization,
        system_prompt=sdk_middleware.system_prompt,
    )


CompactionMiddleware = CLICompactionMiddleware

__all__ = [
    "COMPACTION_FAILURE_PREFIX",
    "CLICompactionMiddleware",
    "CompactionMiddleware",
    "RuntimeModelConfig",
    "_create_cli_compaction_middleware",
]


create_cli_compaction_middleware = _create_cli_compaction_middleware

__all__ = [
    '_create_cli_compaction_middleware',
    'create_cli_compaction_middleware',
]
