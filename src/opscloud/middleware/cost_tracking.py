"""Estimate and persist cumulative model cost for each thread.

The graph owns the durable total. `CostTrackingMiddleware` is the only writer of
`_session_cost_usd`, so each cost update rides the model checkpoint and works for
local, headless, and remote graph execution without a client-side state update.
The client is a reader: it renders the streamed total and never maintains its own
lifetime figure.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
import logging
import math
import operator
import threading
from typing import TYPE_CHECKING, Annotated, Any, NotRequired, TypedDict
from uuid import UUID

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    OmitFromInput,
    PrivateStateAttr,
)
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import LLMResult
from langchain_core.runnables.config import ensure_config
from langgraph.runtime import Runtime
from langgraph.types import Overwrite

from opscloud.middleware.registry import register_middleware
from opscloud.middleware.resume_state import ResumeState
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

SESSION_COST_EVENT_TYPE = "session_cost"
"""Custom-stream event type carrying the thread's absolute cumulative cost."""

_PROVIDER_ALIASES: dict[str, str] = {
    "azure_openai": "azure",
    "bedrock": "aws",
    "google_genai": "google",
    "google_vertexai": "google",
    "mistralai": "mistral",
    "xai": "x-ai",
}
"""Map LangChain provider names to the identifiers used by `genai-prices`."""

_UNPRICEABLE_PROVIDERS: frozenset[str] = frozenset({"openai_codex"})
"""Providers whose access model is not equivalent to per-token API billing."""

_CONFIGURED_PROVIDER_METADATA_KEY = "opscloud_configured_provider"
"""Model metadata key preserving the provider selected by `create_model`."""

_CHECKPOINT_NAMESPACE_METADATA_KEY = "langgraph_checkpoint_ns"
"""Callback metadata key identifying the graph node that made a request."""


def _set_configured_provider_metadata(model: object, provider: str) -> None:
    """Attach the configured provider to every request made by a model."""
    if not provider:
        return
    try:
        current = getattr(model, "metadata", None)
        metadata = dict(current) if isinstance(current, Mapping) else {}
        metadata[_CONFIGURED_PROVIDER_METADATA_KEY] = provider
        model.metadata = metadata  # type: ignore[attr-defined]
    except Exception:
        logger.debug(
            "Could not attach configured provider metadata to %s",
            type(model).__name__,
            exc_info=True,
        )


def _resolve_pricing_provider(
    provider: object,
    fallback_provider: str,
    *,
    prefer_fallback_provider: bool = True,
) -> str:
    """Resolve response metadata without losing a configured provider alias."""
    explicit_provider = provider if isinstance(provider, str) and provider else ""
    resolved_provider = explicit_provider or fallback_provider
    if explicit_provider and not prefer_fallback_provider:
        return explicit_provider
    fallback_provider_key = fallback_provider.strip().lower()
    if fallback_provider_key in _PROVIDER_ALIASES or (
        fallback_provider_key in _UNPRICEABLE_PROVIDERS
    ):
        return fallback_provider
    return resolved_provider


def _token_count(value: object) -> int:
    """Return a non-negative integer token count for a metadata value."""
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else 0
    )


def _cache_write_counts(details: Mapping[str, Any]) -> tuple[int, int, int]:
    """Return generic-only, five-minute, and one-hour cache-write counts."""
    five_minute = _token_count(details.get("ephemeral_5m_input_tokens"))
    one_hour = _token_count(details.get("ephemeral_1h_input_tokens"))
    if five_minute or one_hour:
        return 0, five_minute, one_hour
    generic = _token_count(details.get("cache_creation")) or _token_count(
        details.get("cache_write")
    )
    return generic, 0, 0


def _clamp_cache_counts(
    input_tokens: int,
    cache_read: int,
    cache_writes: tuple[int, int, int],
) -> tuple[int, tuple[int, int, int]]:
    """Clamp disjoint cache buckets to the inclusive input-token total."""
    clamped_read = min(cache_read, input_tokens)
    remaining = input_tokens - clamped_read
    clamped_writes: list[int] = []
    for count in cache_writes:
        clamped = min(count, remaining)
        clamped_writes.append(clamped)
        remaining -= clamped
    return clamped_read, (
        clamped_writes[0],
        clamped_writes[1],
        clamped_writes[2],
    )


def _clamped_detail(
    value: object,
    total: int,
    *,
    field: str,
    model_ref: str,
    provider: str,
) -> int:
    """Return a detail token count clamped to the total that contains it."""
    count = _token_count(value)
    if count <= total:
        return count
    logger.warning(
        "Detail token count exceeds the total containing it; clamping for "
        "pricing. model=%r provider=%r field=%s reported=%d clamped=%d",
        model_ref,
        provider,
        field,
        count,
        total,
    )
    return total


_PRICING_UNAVAILABLE = False
_PRICING_CONTRACT_BROKEN = False
_AUDIO_CACHE_OVERLAP_REPORTED = False


def pricing_data_available() -> bool:
    """Report whether `genai-prices` is currently able to price a request."""
    return not (_PRICING_UNAVAILABLE or _PRICING_CONTRACT_BROKEN)


def _load_pricing() -> tuple[Any, Any] | None:
    """Import `genai-prices` lazily, tracking whether it is currently loadable."""
    global _PRICING_UNAVAILABLE  # noqa: PLW0603
    try:
        from genai_prices import Usage, calc_price
    except Exception:
        if not _PRICING_UNAVAILABLE:
            logger.warning(
                "Could not load genai-prices; cost estimates are unavailable "
                "for this session.",
                exc_info=True,
            )
        _PRICING_UNAVAILABLE = True
        return None
    _PRICING_UNAVAILABLE = False
    return Usage, calc_price


def estimate_cost(
    usage_metadata: Mapping[str, Any] | None,
    model_name: str,
    provider: str = "",
) -> float | None:
    """Estimate one model request's cost in USD from LangChain usage metadata."""
    global _AUDIO_CACHE_OVERLAP_REPORTED, _PRICING_CONTRACT_BROKEN  # noqa: PLW0603
    model_ref = model_name.strip()
    provider_key = provider.strip().lower()
    if not usage_metadata or not model_ref:
        return None
    if provider_key in _UNPRICEABLE_PROVIDERS:
        logger.debug(
            "Cost estimate unavailable for non-API provider=%r model=%r",
            provider,
            model_ref,
        )
        return None

    input_tokens = _token_count(usage_metadata.get("input_tokens"))
    output_tokens = _token_count(usage_metadata.get("output_tokens"))
    if not input_tokens and not output_tokens:
        logger.debug(
            "Usage reports only a combined token total, which cannot be priced: "
            "model=%r provider=%r",
            model_ref,
            provider,
        )
        return None

    input_details = usage_metadata.get("input_token_details")
    if isinstance(input_details, Mapping):
        cache_read_tokens = _token_count(input_details.get("cache_read"))
        cache_writes = _cache_write_counts(input_details)
        input_audio_tokens = _clamped_detail(
            input_details.get("audio"),
            input_tokens,
            field="input audio",
            model_ref=model_ref,
            provider=provider,
        )
    else:
        cache_read_tokens = 0
        cache_writes = (0, 0, 0)
        input_audio_tokens = 0

    output_details = usage_metadata.get("output_token_details")
    if isinstance(output_details, Mapping):
        output_reasoning_tokens = _token_count(output_details.get("reasoning"))
        if provider_key == "perplexity":
            output_tokens += output_reasoning_tokens
        output_audio_tokens = _clamped_detail(
            output_details.get("audio"),
            output_tokens,
            field="output audio",
            model_ref=model_ref,
            provider=provider,
        )
        output_reasoning_tokens = _clamped_detail(
            output_reasoning_tokens,
            output_tokens,
            field="output reasoning",
            model_ref=model_ref,
            provider=provider,
        )
    else:
        output_audio_tokens = 0
        output_reasoning_tokens = 0

    original_cache_read = cache_read_tokens
    original_cache_writes = cache_writes
    cache_read_tokens, cache_writes = _clamp_cache_counts(
        input_tokens, cache_read_tokens, cache_writes
    )
    if (
        cache_read_tokens != original_cache_read
        or cache_writes != original_cache_writes
    ):
        logger.warning(
            "Cache token counts exceed inclusive input total; clamping for pricing."
        )
    generic_cache_write_tokens, cache_write_5m_tokens, cache_write_1h_tokens = (
        cache_writes
    )
    cache_write_tokens = generic_cache_write_tokens or (
        cache_write_5m_tokens + cache_write_1h_tokens
    )

    if input_audio_tokens and (cache_read_tokens or any(cache_writes)):
        if not _AUDIO_CACHE_OVERLAP_REPORTED:
            _AUDIO_CACHE_OVERLAP_REPORTED = True
        input_audio_tokens = 0

    pricing = _load_pricing()
    if pricing is None:
        # Fallback heuristic calculation if genai-prices is unavailable
        rate_in = 0.003
        rate_out = 0.015
        if "gpt-4o-mini" in model_ref.lower():
            rate_in, rate_out = 0.00015, 0.0006
        elif "gpt-4o" in model_ref.lower():
            rate_in, rate_out = 0.0025, 0.01
        elif "gemini" in model_ref.lower():
            rate_in, rate_out = 0.00125, 0.005
        return round((input_tokens / 1000.0) * rate_in + (output_tokens / 1000.0) * rate_out, 6)

    usage_type, calc_price = pricing
    provider_id = _PROVIDER_ALIASES.get(provider_key, provider_key) or None
    try:
        usage = usage_type(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens or None,
            cache_write_tokens=cache_write_tokens or None,
            cache_write_5m_tokens=cache_write_5m_tokens or None,
            cache_write_1h_tokens=cache_write_1h_tokens or None,
            input_audio_tokens=input_audio_tokens or None,
            output_audio_tokens=output_audio_tokens or None,
            output_reasoning_tokens=output_reasoning_tokens or None,
        )
    except Exception:
        if not _PRICING_CONTRACT_BROKEN:
            logger.warning("genai-prices rejected usage schema", exc_info=True)
        _PRICING_CONTRACT_BROKEN = True
        return None
    _PRICING_CONTRACT_BROKEN = False

    try:
        price = calc_price(
            usage,
            model_ref=model_ref,
            provider_id=provider_id,
        )
        cost_usd = float(price.total_price)
    except Exception:
        logger.debug("Cost estimate lookup failed for model=%r provider=%r", model_ref, provider)
        return None

    return cost_usd if math.isfinite(cost_usd) and cost_usd >= 0 else None


def resolve_message_model(
    message: object,
    *,
    fallback_model: str = "",
    fallback_provider: str = "",
    prefer_fallback_provider: bool = True,
) -> tuple[str, str]:
    """Resolve the model and provider attached to a streamed model message."""
    metadata = getattr(message, "response_metadata", None)
    if not isinstance(metadata, Mapping):
        metadata = {}
    model_name = metadata.get("model_name") or metadata.get("model") or fallback_model
    resolved_model = model_name if isinstance(model_name, str) else fallback_model
    provider = metadata.get("model_provider") or metadata.get("provider")
    resolved_provider = _resolve_pricing_provider(
        provider,
        fallback_provider,
        prefer_fallback_provider=prefer_fallback_provider,
    )
    return resolved_model, resolved_provider


_MAX_TRACKED_THREADS = 64
_MAX_RECORDS_PER_THREAD = 1_024
_MAX_INFLIGHT_REQUESTS = 4_096


@dataclass(frozen=True, slots=True)
class _ModelCallRecord:
    message_id: str | None
    usage_metadata: Mapping[str, Any]
    model_name: str
    provider: str
    scope: str = ""


@dataclass(frozen=True, slots=True)
class _ModelCallContext:
    thread_id: str
    configured_provider: str
    scope: str


def _parent_checkpoint_scope(namespace: object) -> str:
    if not isinstance(namespace, str) or not namespace:
        return ""
    return namespace.rpartition("|")[0]


def _owning_checkpoint_scope(scope: str) -> str:
    parts = scope.split("|") if scope else []
    while parts and parts[-1].isdigit():
        parts.pop()
    if parts:
        parts.pop()
    while parts and parts[-1].isdigit():
        parts.pop()
    return "|".join(parts)


class _SessionCostRecorder(BaseCallbackHandler):
    run_inline = True

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._run_contexts: OrderedDict[UUID, _ModelCallContext] = OrderedDict()
        self._records: OrderedDict[str, list[_ModelCallRecord]] = OrderedDict()

    def _start(self, run_id: UUID, metadata: Mapping[str, Any] | None) -> None:
        configurable = ensure_config().get("configurable") or {}
        thread_id = metadata.get("thread_id") if metadata is not None else None
        if not isinstance(thread_id, str) or not thread_id:
            thread_id = configurable.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            with self._lock:
                self._run_contexts.pop(run_id, None)
            return

        configured_provider = (
            metadata.get(_CONFIGURED_PROVIDER_METADATA_KEY)
            if metadata is not None
            else None
        )
        if not isinstance(configured_provider, str):
            configured_provider = ""

        raw_scope = (
            metadata.get(_CHECKPOINT_NAMESPACE_METADATA_KEY)
            if metadata is not None
            else None
        )
        if not isinstance(raw_scope, str) or not raw_scope:
            raw_scope = configurable.get("checkpoint_ns")
        scope = _parent_checkpoint_scope(raw_scope)

        context = _ModelCallContext(
            thread_id=thread_id,
            configured_provider=configured_provider,
            scope=scope,
        )
        with self._lock:
            while len(self._run_contexts) >= _MAX_INFLIGHT_REQUESTS:
                self._run_contexts.popitem(last=False)
            self._run_contexts[run_id] = context

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: UUID,
        metadata: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self._start(run_id, metadata)

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: UUID,
        metadata: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self._start(run_id, metadata)

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        with self._lock:
            context = self._run_contexts.pop(run_id, None)
        if context is None or not context.thread_id:
            return
        try:
            record = _record_from_response(
                response,
                configured_provider=context.configured_provider,
                scope=context.scope,
            )
        except Exception:
            logger.warning("Could not read usage from model response", exc_info=True)
            return
        if record is None:
            return
        with self._lock:
            while context.thread_id not in self._records and (
                len(self._records) >= _MAX_TRACKED_THREADS
            ):
                self._records.popitem(last=False)
            records = self._records.setdefault(context.thread_id, [])
            self._records.move_to_end(context.thread_id)
            records.append(record)
            if len(records) > _MAX_RECORDS_PER_THREAD:
                del records[:-_MAX_RECORDS_PER_THREAD]

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        with self._lock:
            self._run_contexts.pop(run_id, None)

    def drain(
        self,
        thread_id: str,
        *,
        scope: str | None = None,
    ) -> list[_ModelCallRecord]:
        with self._lock:
            records = self._records.get(thread_id, [])
            if scope is None:
                return self._records.pop(thread_id, [])
            claimed = [record for record in records if record.scope == scope]
            remaining = [record for record in records if record.scope != scope]
            if remaining:
                self._records[thread_id] = remaining
            else:
                self._records.pop(thread_id, None)
            return claimed

    def restore(self, thread_id: str, records: list[_ModelCallRecord]) -> None:
        if not records:
            return
        with self._lock:
            existing = self._records.get(thread_id)
            if existing is None:
                while len(self._records) >= _MAX_TRACKED_THREADS:
                    self._records.popitem(last=False)
                existing = []
                self._records[thread_id] = existing
            existing[:0] = records
            if len(existing) > _MAX_RECORDS_PER_THREAD:
                del existing[: len(existing) - _MAX_RECORDS_PER_THREAD]
            self._records.move_to_end(thread_id)


def _record_from_response(
    response: LLMResult,
    *,
    configured_provider: str = "",
    scope: str = "",
) -> _ModelCallRecord | None:
    message: object | None = None
    usage_metadata: object = None
    for generations in response.generations:
        for generation in generations:
            candidate = getattr(generation, "message", None)
            candidate_usage = getattr(candidate, "usage_metadata", None)
            if candidate is not None and candidate_usage:
                message = candidate
                usage_metadata = candidate_usage
    if message is None or not isinstance(usage_metadata, Mapping):
        return None
    model_name, provider = resolve_message_model(
        message,
        fallback_provider=configured_provider,
    )
    message_id = getattr(message, "id", None)
    return _ModelCallRecord(
        message_id=message_id if isinstance(message_id, str) and message_id else None,
        usage_metadata=dict(usage_metadata),
        model_name=model_name,
        provider=provider,
        scope=scope,
    )


_RECORDER = _SessionCostRecorder()
_RECORDER_VAR: ContextVar[_SessionCostRecorder | None] = ContextVar(
    "opscloud_session_cost_recorder",
    default=_RECORDER,
)


def _install_recorder() -> None:
    try:
        from langchain_core.tracers.context import register_configure_hook

        register_configure_hook(_RECORDER_VAR, inheritable=False)
    except Exception:
        pass


_install_recorder()


def _drain_recorded_costs(
    thread_id: str | None,
    *,
    scope: str | None = None,
) -> list[_ModelCallRecord]:
    recorder = _RECORDER_VAR.get()
    if recorder is None or not thread_id:
        return []
    return recorder.drain(thread_id, scope=scope)


def _restore_recorded_costs(
    thread_id: str | None,
    records: list[_ModelCallRecord],
) -> bool:
    if not records:
        return True
    recorder = _RECORDER_VAR.get()
    if recorder is None or not thread_id:
        return False
    recorder.restore(thread_id, records)
    return True


class _CostTransfer(TypedDict):
    owner_scope: str
    cost_usd: float
    tokens: NotRequired[int]


class CostState(ResumeState):
    """Agent state extended with cumulative thread-cost and token channels."""

    _session_cost_usd: Annotated[NotRequired[float], PrivateStateAttr, operator.add]
    _session_total_tokens: Annotated[NotRequired[int], PrivateStateAttr, operator.add]
    _session_cost_transfers: Annotated[
        NotRequired[dict[str, _CostTransfer]],
        OmitFromInput,
        operator.or_,
    ]


def _checkpointed_model_spec(state: CostState) -> tuple[str, str]:
    model_spec = state.get("_model_spec")
    if not isinstance(model_spec, str) or not model_spec:
        return "", ""
    provider, separator, model_name = model_spec.partition(":")
    if not separator:
        return provider, ""
    return model_name, provider


def _pricing_target(
    model_name: str,
    provider: str,
    fallback: tuple[str, str],
) -> tuple[str, str]:
    resolved_model = model_name or fallback[0]
    resolved_provider = provider or fallback[1]
    if not resolved_model:
        from opscloud.config.settings import get_settings

        settings = get_settings()
        resolved_model = getattr(settings, "model_name", "") or ""
        resolved_provider = resolved_provider or getattr(settings, "model_provider", "") or ""
    return resolved_model, resolved_provider


def _thread_id(runtime: Runtime[ContextT]) -> str | None:
    execution_info = getattr(runtime, "execution_info", None)
    thread_id = getattr(execution_info, "thread_id", None)
    return thread_id if isinstance(thread_id, str) and thread_id else None


def _checkpoint_scope(runtime: Runtime[ContextT]) -> str:
    execution_info = getattr(runtime, "execution_info", None)
    return _parent_checkpoint_scope(getattr(execution_info, "checkpoint_ns", None))


def _latest_ai_message(messages: Sequence[Any]) -> AIMessage | None:
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            return message
    return None


@register_middleware(name="cost_tracking")
class CostTrackingMiddleware(AgentMiddleware[CostState, ContextT]):
    """Tracks token usage and estimates costs across turns in a thread.

    Provides durable checkpoint updates and real-time streaming events
    for terminal & TUI displays.
    """

    state_schema = CostState

    def __init__(self, *, nested: bool = False) -> None:
        super().__init__()
        self._nested = nested

    def before_agent(
        self,
        state: CostState,
        runtime: Runtime[ContextT],
    ) -> dict[str, Any] | None:
        if not self._nested:
            return None
        return {
            "_session_cost_usd": Overwrite(0.0),
            "_session_total_tokens": Overwrite(0),
        }

    async def abefore_agent(
        self,
        state: CostState,
        runtime: Runtime[ContextT],
    ) -> dict[str, Any] | None:
        return self.before_agent(state, runtime)

    def after_model(
        self,
        state: CostState,
        runtime: Runtime[ContextT],
    ) -> dict[str, Any] | None:
        try:
            return self._charge(state, runtime, price_latest_message=True)
        except Exception:
            logger.warning("Cost tracking failed to charge a model step", exc_info=True)
            return None

    def after_agent(
        self,
        state: CostState,
        runtime: Runtime[ContextT],
    ) -> dict[str, Any] | None:
        try:
            return self._after_agent_update(state, runtime)
        except Exception:
            logger.warning(
                "Cost tracking failed to charge completed agent run",
                exc_info=True,
            )
            return None

    def _after_agent_update(
        self,
        state: CostState,
        runtime: Runtime[ContextT],
    ) -> dict[str, Any] | None:
        update = self._charge(state, runtime, price_latest_message=False)
        if self._nested:
            prior_usd = state.get("_session_cost_usd")
            if not isinstance(prior_usd, int | float) or not math.isfinite(prior_usd):
                prior_usd = 0.0
            delta_usd = update.get("_session_cost_usd", 0.0) if update else 0.0
            total_usd = max(float(prior_usd), 0.0) + delta_usd

            prior_tokens = state.get("_session_total_tokens")
            if not isinstance(prior_tokens, int) or prior_tokens < 0:
                prior_tokens = 0
            delta_tokens = update.get("_session_total_tokens", 0) if update else 0
            total_tokens = prior_tokens + delta_tokens

            scope = _checkpoint_scope(runtime)
            if scope and (total_usd > 0 or total_tokens > 0):
                transfers: dict[str, _CostTransfer] = dict(
                    state.get("_session_cost_transfers") or {}
                )
                if update:
                    pending = update.get("_session_cost_transfers")
                    if isinstance(pending, Overwrite) and isinstance(pending.value, dict):
                        transfers = dict(pending.value)
                transfer_entry: _CostTransfer = {
                    "owner_scope": _owning_checkpoint_scope(scope),
                    "cost_usd": total_usd,
                    "tokens": total_tokens,
                }
                transfers[scope] = transfer_entry
                if update is None:
                    update = {}
                update["_session_cost_transfers"] = Overwrite(transfers)
        return update

    def _charge(
        self,
        state: CostState,
        runtime: Runtime[ContextT],
        *,
        price_latest_message: bool,
    ) -> dict[str, Any] | None:
        thread_id = _thread_id(runtime)
        fallback = _checkpointed_model_spec(state)
        message = (
            _latest_ai_message(state.get("messages") or [])
            if price_latest_message
            else None
        )
        main_message_id = message.id if message is not None else None
        delta_usd = 0.0
        delta_tokens = 0
        transfers = state.get("_session_cost_transfers") or {}
        remaining_transfers = dict(transfers)
        owner_scope = _checkpoint_scope(runtime)
        claimed_transfer = False
        for source_scope, transfer in transfers.items():
            if (
                isinstance(source_scope, str)
                and isinstance(transfer, Mapping)
                and transfer.get("owner_scope") == owner_scope
                and isinstance(transfer.get("cost_usd"), int | float)
                and math.isfinite(transfer["cost_usd"])
                and transfer["cost_usd"] > 0
            ):
                delta_usd += float(transfer["cost_usd"])
                trans_toks = transfer.get("tokens", 0)
                if isinstance(trans_toks, int) and trans_toks > 0:
                    delta_tokens += trans_toks
                remaining_transfers.pop(source_scope, None)
                claimed_transfer = True
        charged_message_ids: set[str] = set()
        charged_count = 0
        pricing_attempted = False
        scope = _checkpoint_scope(runtime) if self._nested else None
        drained = _drain_recorded_costs(thread_id, scope=scope)
        try:
            for record in drained:
                provider = record.provider
                if main_message_id is not None and record.message_id == main_message_id:
                    provider = _resolve_pricing_provider(provider, fallback[1])
                pricing_attempted = True
                cost_usd = estimate_cost(
                    record.usage_metadata,
                    *_pricing_target(record.model_name, provider, fallback),
                )
                inp = _token_count(record.usage_metadata.get("input_tokens"))
                out = _token_count(record.usage_metadata.get("output_tokens"))
                delta_tokens += inp + out
                if cost_usd is None:
                    continue
                delta_usd += cost_usd
                charged_count += 1
                if record.message_id is not None:
                    charged_message_ids.add(record.message_id)
            if price_latest_message:
                already_charged = (
                    message.id in charged_message_ids
                    if message is not None and message.id is not None
                    else charged_count > 0
                )
                if message is not None and not already_charged:
                    model_name, provider = resolve_message_model(
                        message,
                        fallback_model=fallback[0],
                        fallback_provider=fallback[1],
                    )
                    pricing_attempted = True
                    cost_usd = estimate_cost(
                        getattr(message, "usage_metadata", None),
                        *_pricing_target(model_name, provider, fallback),
                    )
                    usage = getattr(message, "usage_metadata", None)
                    if isinstance(usage, Mapping):
                        inp = _token_count(usage.get("input_tokens"))
                        out = _token_count(usage.get("output_tokens"))
                        delta_tokens += inp + out
                    if cost_usd is not None:
                        delta_usd += cost_usd

            if not self._nested and (delta_usd > 0 or delta_tokens > 0 or pricing_attempted):
                pricing_ok = pricing_data_available()
                if delta_usd > 0 or delta_tokens > 0 or not pricing_ok:
                    self._emit_total(
                        state,
                        runtime,
                        delta_usd,
                        delta_tokens=delta_tokens,
                        pricing_ok=pricing_ok,
                    )
            if delta_usd <= 0 and delta_tokens <= 0 and not claimed_transfer:
                return None
            update: dict[str, Any] = {}
            if claimed_transfer:
                update["_session_cost_transfers"] = Overwrite(remaining_transfers)
            if delta_usd > 0:
                update["_session_cost_usd"] = delta_usd
            if delta_tokens > 0:
                update["_session_total_tokens"] = delta_tokens
        except BaseException:
            _restore_recorded_costs(thread_id, drained)
            raise
        return update

    @staticmethod
    def _emit_total(
        state: CostState,
        runtime: Runtime[ContextT],
        delta_usd: float,
        *,
        delta_tokens: int = 0,
        pricing_ok: bool,
    ) -> None:
        writer = getattr(runtime, "stream_writer", None)
        if not callable(writer):
            return
        prior_usd = state.get("_session_cost_usd")
        if not isinstance(prior_usd, int | float) or not math.isfinite(prior_usd):
            prior_usd = 0.0
        prior_tokens = state.get("_session_total_tokens")
        if not isinstance(prior_tokens, int) or prior_tokens < 0:
            prior_tokens = 0
        total_tokens = prior_tokens + delta_tokens
        try:
            writer(
                {
                    "type": SESSION_COST_EVENT_TYPE,
                    "total": max(float(prior_usd), 0.0) + delta_usd,
                    "total_tokens": total_tokens,
                    "thread_id": _thread_id(runtime) or "",
                    "pricing_ok": pricing_ok,
                }
            )
        except Exception:
            logger.debug("Could not emit session cost event", exc_info=True)


__all__ = [
    "SESSION_COST_EVENT_TYPE",
    "CostState",
    "CostTrackingMiddleware",
    "estimate_cost",
    "pricing_data_available",
    "resolve_message_model",
]
