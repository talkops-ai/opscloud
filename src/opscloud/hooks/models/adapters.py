"""Cached runtime validators for hook model boundaries."""

from pydantic import TypeAdapter

from opscloud.hooks.models.domain import (
    HookDecision,
    HookDomainEvent,
    HookInvocation,
)
from opscloud.hooks.models.transport import (
    HookInvocationRequest,
    HookInvocationResponse,
)

HOOK_DOMAIN_EVENT_ADAPTER: TypeAdapter[HookDomainEvent] = TypeAdapter(HookDomainEvent)
HOOK_INVOCATION_ADAPTER: TypeAdapter[HookInvocation] = TypeAdapter(HookInvocation)
HOOK_DECISION_ADAPTER: TypeAdapter[HookDecision] = TypeAdapter(HookDecision)
HOOK_INVOCATION_REQUEST_ADAPTER: TypeAdapter[HookInvocationRequest] = TypeAdapter(HookInvocationRequest)
HOOK_INVOCATION_RESPONSE_ADAPTER: TypeAdapter[HookInvocationResponse] = TypeAdapter(HookInvocationResponse)
