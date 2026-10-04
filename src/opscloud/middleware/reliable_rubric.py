"""Rubric middleware with transport retry, event streaming, and isolated grading.

``ReliableRubricMiddleware`` extends the SDK's ``RubricMiddleware`` with:

- **Isolated Grader Agent**: Creates a dedicated evaluator subagent configured
  with read-only evidence tools, budget guards, and custom system prompts.
- **Transport Resilience**: Catches transient HTTP read and remote protocol errors
  (e.g., during long grading responses) and retries the grader invocation once
  without replaying worker operations.
- **Control Message Sanitization**: Strips internal goal-state notices, continuation
  pings, and past evaluation metadata so the grader evaluates only genuine work.
- **Streaming Telemetry**: Emits ``rubric_evaluation_start`` and ``rubric_evaluation_end``
  lifecycle events via ``runtime.stream_writer`` for real-time terminal display.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Callable,
    NotRequired,
    cast,
)
import warnings

import httpx
from deepagents.middleware.rubric import (
    RUBRIC_GRADER_MESSAGE_SOURCE,
    GraderResponse,
    RubricMiddleware as BaseRubricMiddleware,
    RubricState,
)
import deepagents.middleware.rubric as _rubric_mod
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    PrivateStateAttr,
    hook_config,
)
from langchain_core.messages import AnyMessage, HumanMessage
from langgraph.errors import GraphBubbleUp

from opscloud.middleware.goal_state_notice import is_conversation_control_message
from opscloud.middleware.registry import register_middleware
from opscloud.utils.logger import get_logger

if TYPE_CHECKING:
    from deepagents.middleware.rubric import RubricEvaluation
    from langchain_core.language_models import BaseChatModel
    from langchain_core.tools import BaseTool
    from langgraph.runtime import Runtime

logger = get_logger(__name__)

_strategy_from_result = getattr(_rubric_mod, "_strategy_from_result", None)

__all__ = ["ReliableRubricMiddleware", "ReliableRubricState", "RubricGraderState", "RubricMiddleware"]


# ---------------------------------------------------------------------------
# Transport Error Recovery Helpers
# ---------------------------------------------------------------------------


def _exception_chain(exc: BaseException) -> Iterator[BaseException]:
    """Yield an exception, its causes, context, and group members without cycles."""
    pending = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        curr_id = id(current)
        if curr_id in seen:
            continue
        seen.add(curr_id)
        yield current
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        elif current.__context__ is not None:
            pending.append(current.__context__)


def _is_transient_grader_transport_error(exc: BaseException) -> bool:
    """Determine whether an evaluation exception is a retryable network drop."""
    for current in _exception_chain(exc):
        if isinstance(
            current,
            (
                httpx.ReadError,
                httpx.RemoteProtocolError,
                httpx.TimeoutException,
                httpx.ConnectError,
                ConnectionError,
                TimeoutError,
            ),
        ):
            return True
        mod_name = type(current).__module__
        cls_name = type(current).__name__
        if mod_name.startswith("httpcore") and cls_name in {
            "ReadError",
            "RemoteProtocolError",
            "ConnectError",
            "TimeoutException",
        }:
            return True
        if (
            mod_name == "aiohttp.http_exceptions"
            and cls_name == "TransferEncodingError"
            and "Not enough data" in str(current)
        ):
            return True
        err_msg = str(current)
        if any(code in err_msg for code in ("429", "503", "Connection reset by peer")):
            return True
    return False


# ---------------------------------------------------------------------------
# Message History Sanitization
# ---------------------------------------------------------------------------


def _without_internal_control_messages(state: RubricState) -> RubricState:
    """Filter out internal goal-state notices so the grader evaluates only real work."""
    messages = state.get("messages", [])
    if not isinstance(messages, list):
        return state
    filtered: list[AnyMessage] = [
        msg for msg in messages if not is_conversation_control_message(msg)
    ]
    if len(filtered) == len(messages):
        return state
    updated = dict(state)
    updated["messages"] = filtered
    return cast("RubricState", updated)


# ---------------------------------------------------------------------------
# State Schemas
# ---------------------------------------------------------------------------


class ReliableRubricState(RubricState):
    """Rubric state carrying OpsCloud's private runtime model selections."""

    _model_spec: Annotated[NotRequired[str], PrivateStateAttr]
    """Active chat model specifier (written by ConfigurableModelMiddleware)."""

    _model_params: Annotated[NotRequired[dict[str, Any] | None], PrivateStateAttr]
    """Model hyperparameters belonging to _model_spec."""

    _rubric_model_spec: Annotated[NotRequired[str], PrivateStateAttr]
    """Thread-scoped grader model override selected by the operator."""


class RubricGraderState(AgentState[GraderResponse]):
    """Private state for the isolated nested grader agent."""

    rubric_grading_operation_id: NotRequired[str]


def _is_smart_mode(context: object = None, store: object = None) -> bool:
    """Synchronously determine whether Smart mode is active."""
    try:
        from opscloud.middleware.jev_model_router import _is_smart_mode_sync

        return _is_smart_mode_sync(context, store=store)
    except Exception:
        return False


async def _ais_smart_mode(context: object = None, store: object = None) -> bool:
    """Asynchronously determine whether Smart mode is active."""
    try:
        from opscloud.middleware.jev_model_router import _ais_smart_mode as _async_smart

        return await _async_smart(context, store=store)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Middleware Implementation
# ---------------------------------------------------------------------------


with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message="The middleware `RubricMiddleware` is in beta",
        category=Warning,
    )

    @register_middleware(name="reliable_rubric")
    class ReliableRubricMiddleware(BaseRubricMiddleware):
        """Reliable, isolated rubric grader with transport retry and telemetry streaming."""

        state_schema = ReliableRubricState

        def __init__(
            self,
            *,
            model: str | BaseChatModel | None = None,
            system_prompt: str | None = None,
            tools: Sequence[BaseTool] | None = None,
            grader_middleware: Sequence[AgentMiddleware[Any, Any]] | None = None,
            grader_context_schema: type[Any] | None = None,
            max_iterations: int = 3,
            on_evaluation: Callable[[RubricEvaluation], None] | None = None,
            inherit_main_model: bool = False,
            runtime_bootstrap_model: str | BaseChatModel | None = None,
        ) -> None:
            kwargs: dict[str, Any] = {
                "model": model or "anthropic:claude-3-5-haiku-20241022",
                "max_iterations": max_iterations,
            }
            if system_prompt is not None:
                kwargs["system_prompt"] = system_prompt
            if tools is not None:
                kwargs["tools"] = tools
            if on_evaluation is not None:
                kwargs["on_evaluation"] = on_evaluation

            super().__init__(**kwargs)
            self._grader_model_spec = model or "anthropic:claude-3-5-haiku-20241022"
            self._grader_middleware = list(grader_middleware or ())
            self._grader_context_schema = grader_context_schema
            self._inherit_main_model = inherit_main_model
            self._runtime_bootstrap_model = runtime_bootstrap_model
            self._runtime_grader_model: ContextVar[str | None] = ContextVar(
                "runtime_grader_model",
                default=None,
            )
            self._jev_grader: Any | None = None

        def _get_jev_grader(self) -> Any:
            """Lazily instantiate JevHybridRubricGrader when needed."""
            if self._jev_grader is None:
                from opscloud.rubrics.jev_grader import JevHybridRubricGrader

                self._jev_grader = JevHybridRubricGrader()
            return self._jev_grader

        @contextmanager
        def _runtime_grader_trace(self, model_label: str | None) -> Iterator[None]:
            token = self._runtime_grader_model.set(model_label)
            try:
                yield
            finally:
                self._runtime_grader_model.reset(token)

        def _ensure_grader(self) -> Any:
            """Instantiate the isolated rubric grader agent graph."""
            if self._grader is not None:
                return self._grader

            from deepagents._models import resolve_model  # noqa: PLC2701
            from langchain.agents import create_agent

            resolved_model = resolve_model(self._model)
            self._resolved_model = resolved_model

            logger.debug(
                "ReliableRubric._ensure_grader: building isolated grader agent with middleware: %s",
                [getattr(m, "name", type(m).__name__) for m in self._grader_middleware],
            )

            self._grader = create_agent(
                model=resolved_model,
                system_prompt=self._system_prompt,
                tools=self._tools,
                middleware=self._grader_middleware,
                name=RUBRIC_GRADER_MESSAGE_SOURCE,
                response_format=GraderResponse,
                state_schema=RubricGraderState,
                context_schema=self._grader_context_schema,
            )
            return self._grader

        def _emit_stream_event(self, runtime: Runtime[Any], payload: dict[str, Any]) -> None:
            """Safely emit an evaluation event to the live stream writer."""
            writer = getattr(runtime, "stream_writer", None)
            if writer is not None and callable(writer):
                try:
                    writer(payload)
                except Exception:
                    logger.debug("Failed to emit rubric stream event %s", payload, exc_info=True)

        def _grader_input(
            self,
            state: RubricState,
            iteration: int,
            correction: str | None = None,
        ) -> dict[str, Any]:
            grading_run_id = state.get("_current_grading_run_id") or "untracked"
            sanitized_state = _without_internal_control_messages(state)
            payload = self._build_grader_payload(sanitized_state, iteration, correction)
            return {
                "messages": [HumanMessage(content=payload)],
                "rubric_grading_operation_id": f"{grading_run_id}:{iteration}",
            }

        def _grade_once(
            self,
            state: RubricState,
            iteration: int,
            correction: str | None = None,
            *,
            context: object | None = None,
        ) -> GraderResponse:
            grader = self._ensure_grader()
            meta_fn = getattr(self, "_grader_trace_metadata", lambda **kw: {})
            rec_fn = getattr(self, "_record_grader_trace_metadata", lambda m: None)
            cfg_fn = getattr(self, "_grader_invocation_config", lambda m: {})

            metadata = meta_fn()
            rec_fn(metadata)
            result = grader.invoke(
                self._grader_input(state, iteration, correction),
                config=cfg_fn(metadata),
                context=context,
            )
            strategy = _strategy_from_result(result) if _strategy_from_result is not None else None
            rec_fn(meta_fn(effective_strategy=strategy))
            return self._extract_graded(result)

        async def _agrade_once(
            self,
            state: RubricState,
            iteration: int,
            correction: str | None = None,
            *,
            context: object | None = None,
        ) -> GraderResponse:
            grader = self._ensure_grader()
            meta_fn = getattr(self, "_grader_trace_metadata", lambda **kw: {})
            rec_fn = getattr(self, "_record_grader_trace_metadata", lambda m: None)
            cfg_fn = getattr(self, "_grader_invocation_config", lambda m: {})

            metadata = meta_fn()
            rec_fn(metadata)
            result = await grader.ainvoke(
                self._grader_input(state, iteration, correction),
                config=cfg_fn(metadata),
                context=context,
            )
            strategy = _strategy_from_result(result) if _strategy_from_result is not None else None
            rec_fn(meta_fn(effective_strategy=strategy))
            return self._extract_graded(result)

        @staticmethod
        def _context(context: object | None) -> Any:
            """Copy the parent runtime context into the nested grader schema."""
            from opscloud.agent.config import CLIContextSchema

            inner = getattr(context, "context", None) if not isinstance(context, (CLIContextSchema, dict)) else context
            target = inner if inner is not None else context

            if isinstance(target, CLIContextSchema):
                return replace(
                    target,
                    model_params=dict(target.model_params),
                    profile_overrides=dict(target.profile_overrides),
                    hooks_server_events=list(target.hooks_server_events),
                )
            if isinstance(target, dict):
                parsed = CLIContextSchema.from_payload(target)
                if parsed is not None:
                    return parsed
            return CLIContextSchema()

        def _grader_context(
            self, state: ReliableRubricState, context: object | None
        ) -> Any:
            """Select the effective grader model without mutating shared middleware."""
            from opscloud.middleware.resume_state import INHERIT_RUBRIC_MODEL, coerce_model_spec

            grader_context = self._context(context)
            selected = coerce_model_spec(state.get("_rubric_model_spec"))
            inherit = selected == INHERIT_RUBRIC_MODEL or (
                selected is None and self._inherit_main_model
            )
            if inherit:
                main_model = coerce_model_spec(state.get("_model_spec"))
                if main_model is not None and hasattr(grader_context, "model"):
                    grader_context.model = main_model
                    params = state.get("_model_params")
                    if isinstance(params, Mapping) and hasattr(grader_context, "model_params"):
                        grader_context.model_params = dict(params)
            else:
                if hasattr(grader_context, "model"):
                    grader_context.model = selected
                if hasattr(grader_context, "model_params"):
                    grader_context.model_params = {}
            return grader_context

        def _invoke_grader(
            self,
            state: RubricState,
            iteration: int,
            correction: str | None = None,
            *,
            context: object | None = None,
        ) -> GraderResponse:
            """Invoke the isolated grader with Smart-mode Jev fast-pass and LLM fallback."""
            reliable_state = cast("ReliableRubricState", state)
            grader_context = self._grader_context(reliable_state, context)

            # ── Smart Mode Path (Jev System One Fast-Pass) ──
            jev_grader = self._get_jev_grader()
            raw_rubric = state.get("rubric") or state.get("_goal_rubric") or state.get("_sticky_rubric") or ""
            store = getattr(context, "store", None)
            is_smart = _is_smart_mode(context, store=store) or _is_smart_mode(grader_context)
            if is_smart and jev_grader.is_available() and raw_rubric:
                try:
                    from opscloud.rubrics.jev_compiler import JevCriteriaCompiler

                    objective = str(state.get("_goal_objective") or state.get("goal") or "Fulfill user requirements")
                    compiled = JevCriteriaCompiler.compile(objective, str(raw_rubric))
                    logger.info("ReliableRubricMiddleware: Smart mode active — evaluating criteria via Jev System One")
                    def _targeted_diagnostic(failing: list[str]) -> GraderResponse:
                        """Generate concise diagnostic remediation feedback in a single 1-shot LLM completion without tools."""
                        failing_text = "\n".join(f"- {item}" for item in failing)
                        try:
                            from deepagents._models import resolve_model
                            from langchain_core.messages import HumanMessage, SystemMessage

                            model_spec = getattr(grader_context, "model", None) or getattr(self, "_grader_model_spec", None) or "anthropic:claude-3-5-haiku-20241022"
                            chat_model = resolve_model(model_spec) if isinstance(model_spec, str) else model_spec

                            diagnostic_prompt = [
                                SystemMessage(
                                    content=(
                                        "You are the OpsCloud Rubric Diagnostic Evaluator. Acceptance criteria evaluation failed. "
                                        "Write concise, actionable remediation advice for what specific files, configurations, "
                                        "or commands the agent must execute to satisfy the failing criteria. Do not repeat all criteria."
                                    )
                                ),
                                HumanMessage(
                                    content=(
                                        f"Objective: {objective}\n\n"
                                        f"Unsatisfied criteria:\n{failing_text}\n\n"
                                        "Provide 1-2 concise bullet points explaining exactly what is missing and how to fix it."
                                    )
                                ),
                            ]
                            res = chat_model.invoke(diagnostic_prompt)
                            advice = getattr(res, "content", "") or str(res)
                            explanation = (
                                f"{len(failing)} of {len(compiled.criteria_map)} criteria not yet satisfied.\n\n"
                                f"Remediation Guidance:\n{advice}"
                            )
                        except Exception as diag_exc:
                            logger.warning("Tier 2 diagnostic prompt failed (%s); using default explanation", diag_exc)
                            explanation = f"{len(failing)} criteria not yet satisfied: {', '.join(failing)}"

                        return GraderResponse(
                            result="needs_revision",
                            explanation=explanation,
                            criteria=[],
                        )

                    return jev_grader.grade(
                        compiled,
                        evidence=state,
                        fallback_llm_fn=_targeted_diagnostic,
                    )
                except Exception as jev_exc:
                    logger.warning("Jev rubric evaluation failed (%s); falling back to standard LLM grader", jev_exc)

            # ── Standard Path (Manual, Auto, or non-Jev) ──
            model_label = getattr(grader_context, "model", None)
            with self._runtime_grader_trace(model_label):
                try:
                    return self._grade_once(state, iteration, correction, context=grader_context)
                except Exception as exc:
                    if not _is_transient_grader_transport_error(exc):
                        raise
                    logger.warning("Rubric grader transient error; retrying grading invocation once: %s", exc)
                return self._grade_once(state, iteration, correction, context=grader_context)

        async def _ainvoke_grader(
            self,
            state: RubricState,
            iteration: int,
            correction: str | None = None,
            *,
            context: object | None = None,
        ) -> GraderResponse:
            """Invoke the isolated grader asynchronously with Smart-mode Jev fast-pass and LLM fallback."""
            reliable_state = cast("ReliableRubricState", state)
            grader_context = self._grader_context(reliable_state, context)

            # ── Smart Mode Path (Jev System One Fast-Pass) ──
            jev_grader = self._get_jev_grader()
            raw_rubric = state.get("rubric") or state.get("_goal_rubric") or state.get("_sticky_rubric") or ""
            store = getattr(context, "store", None)
            is_smart = (await _ais_smart_mode(context, store=store)) or (await _ais_smart_mode(grader_context))
            if is_smart and jev_grader.is_available() and raw_rubric:
                try:
                    from opscloud.rubrics.jev_compiler import JevCriteriaCompiler

                    objective = str(state.get("_goal_objective") or state.get("goal") or "Fulfill user requirements")
                    compiled = JevCriteriaCompiler.compile(objective, str(raw_rubric))
                    logger.info("ReliableRubricMiddleware: Smart mode active — evaluating criteria via Jev System One")

                    async def _atargeted_diagnostic(failing: list[str]) -> GraderResponse:
                        """Generate concise diagnostic remediation feedback in a single 1-shot LLM completion without tools."""
                        failing_text = "\n".join(f"- {item}" for item in failing)
                        try:
                            from deepagents._models import resolve_model
                            from langchain_core.messages import HumanMessage, SystemMessage

                            model_spec = getattr(grader_context, "model", None) or getattr(self, "_grader_model_spec", None) or "anthropic:claude-3-5-haiku-20241022"
                            chat_model = resolve_model(model_spec) if isinstance(model_spec, str) else model_spec

                            diagnostic_prompt = [
                                SystemMessage(
                                    content=(
                                        "You are the OpsCloud Rubric Diagnostic Evaluator. Acceptance criteria evaluation failed. "
                                        "Write concise, actionable remediation advice for what specific files, configurations, "
                                        "or commands the agent must execute to satisfy the failing criteria. Do not repeat all criteria."
                                    )
                                ),
                                HumanMessage(
                                    content=(
                                        f"Objective: {objective}\n\n"
                                        f"Unsatisfied criteria:\n{failing_text}\n\n"
                                        "Provide 1-2 concise bullet points explaining exactly what is missing and how to fix it."
                                    )
                                ),
                            ]
                            res = await chat_model.ainvoke(diagnostic_prompt)
                            advice = getattr(res, "content", "") or str(res)
                            explanation = (
                                f"{len(failing)} of {len(compiled.criteria_map)} criteria not yet satisfied.\n\n"
                                f"Remediation Guidance:\n{advice}"
                            )
                        except Exception as diag_exc:
                            logger.warning("Tier 2 diagnostic prompt failed (%s); using default explanation", diag_exc)
                            explanation = f"{len(failing)} criteria not yet satisfied: {', '.join(failing)}"

                        return GraderResponse(
                            result="needs_revision",
                            explanation=explanation,
                            criteria=[],
                        )

                    return await jev_grader.agrade(
                        compiled,
                        evidence=state,
                        fallback_llm_fn=_atargeted_diagnostic,
                    )
                except Exception as jev_exc:
                    logger.warning("Jev async rubric evaluation failed (%s); falling back to standard LLM grader", jev_exc)

            # ── Standard Path (Manual, Auto, or non-Jev) ──
            model_label = getattr(grader_context, "model", None)
            with self._runtime_grader_trace(model_label):
                try:
                    return await self._agrade_once(state, iteration, correction, context=grader_context)
                except Exception as exc:
                    if not _is_transient_grader_transport_error(exc):
                        raise
                    logger.warning("Rubric grader transient async error; retrying grading invocation once: %s", exc)
                return await self._agrade_once(state, iteration, correction, context=grader_context)

        def _grade(
            self,
            state: RubricState,
            iteration: int,
            *,
            context: object | None = None,
        ) -> GraderResponse:
            return super()._grade(state, iteration, context=context)

        async def _agrade(
            self,
            state: RubricState,
            iteration: int,
            *,
            context: object | None = None,
        ) -> GraderResponse:
            return await super()._agrade(state, iteration, context=context)

        def _finalize_evaluation(
            self,
            graded: GraderResponse,
            state: RubricState,
            runtime: Runtime[Any],
            grading_run_id: str,
            iteration: int,
        ) -> dict[str, Any]:
            update = super()._finalize_evaluation(
                graded, state, runtime, grading_run_id, iteration
            )
            # If Jev classified an environmental blocker or ambiguous requirement, mark goal as blocked
            blocker = getattr(graded, "blocker_type", None)
            explanation = getattr(graded, "explanation", "") or getattr(graded, "feedback", "") or ""
            if blocker in {"external_blocker", "ambiguous_requirement"} or "Environmental blocker detected" in str(explanation):
                update["_goal_status"] = "blocked"
                update["_goal_status_note"] = str(explanation)
            return update

        @hook_config(can_jump_to=["model"])
        def after_agent(
            self,
            state: RubricState,
            runtime: Runtime[Any],
        ) -> dict[str, Any] | None:
            prep = self._prepare_evaluation(state, runtime)
            if prep is None:
                return None
            grading_run_id, iteration = prep

            try:
                graded = self._grade(
                    state,
                    iteration,
                    context=runtime,
                )
            except GraphBubbleUp:
                raise
            except Exception as exc:
                return self._handle_grader_exception(
                    runtime,
                    state,
                    grading_run_id,
                    iteration,
                    exc,
                )

            feedback = getattr(graded, "feedback", None) or getattr(graded, "explanation", "")

            if "Environmental blocker detected" in str(feedback):
                self._emit_stream_event(
                    runtime,
                    {
                        "type": "rubric_blocker_detected",
                        "iteration": iteration,
                        "explanation": str(feedback),
                    },
                )

            return self._finalize_evaluation(
                graded,
                state,
                runtime,
                grading_run_id,
                iteration,
            )

        async def aafter_agent(
            self,
            state: RubricState,
            runtime: Runtime[Any],
        ) -> dict[str, Any] | None:
            prep = self._prepare_evaluation(state, runtime)
            if prep is None:
                return None
            grading_run_id, iteration = prep

            try:
                graded = await self._agrade(
                    state,
                    iteration,
                    context=runtime,
                )
            except GraphBubbleUp:
                raise
            except Exception as exc:
                return self._handle_grader_exception(
                    runtime,
                    state,
                    grading_run_id,
                    iteration,
                    exc,
                )

            feedback = getattr(graded, "feedback", None) or getattr(graded, "explanation", "")

            if "Environmental blocker detected" in str(feedback):
                self._emit_stream_event(
                    runtime,
                    {
                        "type": "rubric_blocker_detected",
                        "iteration": iteration,
                        "explanation": str(feedback),
                    },
                )

            return self._finalize_evaluation(
                graded,
                state,
                runtime,
                grading_run_id,
                iteration,
            )


RubricMiddleware = ReliableRubricMiddleware
