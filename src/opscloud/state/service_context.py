"""Service management and plugin state channels for OpsCloud.

Provides generic, extensible state contracts for plugins and skills to manage
live services (containers, clusters, serverless, databases, ingress, networking)
efficiently across any cloud provider.

Allows dynamic plugins to:
- Register and update managed service targets (health, replicas, status, metrics).
- Maintain an active service focus for zero-context-switch triage.
- Record an audit log of operational actions (restarts, scaling, rollouts, patches).
- Persist plugin-private state across checkpoints without cross-plugin collision.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import (
    Annotated,
    Any,
    Literal,
    NotRequired,
    TypedDict,
    cast,
    get_args,
)

from langchain.agents.middleware.types import (
    AgentState,
    PrivateStateAttr,
)

ServiceStatus = Literal[
    "healthy",
    "degraded",
    "failing",
    "updating",
    "stopped",
    "unknown",
]
"""Lifecycle and health status of a managed cloud service."""

_SERVICE_STATUS_VALUES: frozenset[str] = frozenset(get_args(ServiceStatus))

ServiceOperationType = Literal[
    "inspect",
    "restart",
    "scale",
    "deploy",
    "rollback",
    "drain",
    "cordon",
    "config_patch",
    "traffic_shift",
    "custom",
]
"""Standard operational actions performed on services."""


class ManagedService(TypedDict, total=False):
    """Normalized snapshot of a live managed service."""

    id: str
    """Unique canonical identifier (e.g. 'k8s:prod/payments' or ECS ARN)."""

    name: str
    """Human-friendly service name (e.g. 'payments-api')."""

    provider: str
    """Cloud or platform provider (e.g. 'aws', 'kubernetes', 'azure', 'gcp')."""

    service_type: str
    """Kind of service (e.g. 'deployment', 'ecs_service', 'rds_database', 'lambda')."""

    environment: str | None
    """Deployment tier (e.g. 'production', 'staging', 'dev')."""

    status: ServiceStatus
    """Health/operational status."""

    health_summary: str | None
    """Brief human-readable health details (e.g. '3/3 pods ready, 0 restarts')."""

    metrics_summary: str | None
    """Key operational metrics (e.g. 'p99: 45ms, error_rate: 0.02%')."""

    last_action: str | None
    """Last action taken on this service (e.g. 'scaled replicas to 5')."""

    updated_at: str
    """ISO timestamp of last update."""

    metadata: dict[str, Any]
    """Plugin-extensible arbitrary metadata (cluster, namespace, image, port, etc.)."""


class ServiceOperationRecord(TypedDict):
    """Audit record of an operational action executed on a managed service."""

    operation_id: str
    service_id: str
    action: ServiceOperationType | str
    status: Literal["started", "succeeded", "failed", "pending_approval"]
    details: str
    initiated_by: str
    timestamp: str


def _merge_managed_services(
    existing: dict[str, ManagedService] | None,
    new: dict[str, ManagedService] | None,
) -> dict[str, ManagedService]:
    """Reducer that merges managed services by service ID, updating changed fields."""
    if not existing:
        return dict(new or {})
    if not new:
        return dict(existing)

    merged = dict(existing)
    for service_id, new_svc in new.items():
        if service_id in merged:
            current = dict(merged[service_id])
            current.update(new_svc)
            merged[service_id] = cast(ManagedService, current)
        else:
            merged[service_id] = new_svc
    return merged


def _merge_operations_log(
    existing: list[ServiceOperationRecord] | None,
    new: list[ServiceOperationRecord] | None,
) -> list[ServiceOperationRecord]:
    """Accumulating reducer that appends service operations in chronological order."""
    if not existing:
        return list(new or [])
    if not new:
        return list(existing)

    seen_ids = {op["operation_id"] for op in existing}
    result = list(existing)
    for op in new:
        if op["operation_id"] not in seen_ids:
            seen_ids.add(op["operation_id"])
            result.append(op)
    return result


def _merge_plugin_state(
    existing: dict[str, Any] | None,
    new: dict[str, Any] | None,
) -> dict[str, Any]:
    """Reducer that deep-merges plugin-scoped namespaces without cross-plugin clobbering."""
    if not existing:
        return dict(new or {})
    if not new:
        return dict(existing)

    merged = dict(existing)
    for plugin_name, plugin_data in new.items():
        if isinstance(plugin_data, dict) and isinstance(merged.get(plugin_name), dict):
            merged[plugin_name] = {**merged[plugin_name], **plugin_data}
        else:
            merged[plugin_name] = plugin_data
    return merged


def _merge_unique_strings(
    existing: list[str] | None,
    new: list[str] | None,
) -> list[str]:
    """Reducer that accumulates unique string names preserving order."""
    if not existing:
        return list(new or [])
    if not new:
        return list(existing)
    seen = set(existing)
    result = list(existing)
    for item in new:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def make_service_record(
    id: str,
    name: str,
    provider: str,
    service_type: str,
    *,
    environment: str | None = None,
    status: ServiceStatus = "unknown",
    health_summary: str | None = None,
    metrics_summary: str | None = None,
    last_action: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ManagedService:
    """Helper to construct a validated ManagedService instance."""
    return {
        "id": id,
        "name": name,
        "provider": provider,
        "service_type": service_type,
        "environment": environment,
        "status": status if status in _SERVICE_STATUS_VALUES else "unknown",
        "health_summary": health_summary,
        "metrics_summary": metrics_summary,
        "last_action": last_action,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "metadata": dict(metadata or {}),
    }


def make_operation_record(
    operation_id: str,
    service_id: str,
    action: ServiceOperationType | str,
    *,
    status: Literal["started", "succeeded", "failed", "pending_approval"] = "succeeded",
    details: str = "",
    initiated_by: str = "agent",
) -> ServiceOperationRecord:
    """Helper to construct a ServiceOperationRecord."""
    return {
        "operation_id": operation_id,
        "service_id": service_id,
        "action": action,
        "status": status,
        "details": details,
        "initiated_by": initiated_by,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


class ServiceContextState(AgentState):
    """State channels for managing live cloud services and plugin runtimes."""

    _managed_services: Annotated[
        NotRequired[dict[str, ManagedService]],
        PrivateStateAttr,
        _merge_managed_services,
    ]
    """Registry of live cloud services currently managed, monitored, or inspected."""

    _active_service_id: Annotated[
        NotRequired[str | None],
        PrivateStateAttr,
    ]
    """ID of the service currently in focus for troubleshooting or operational commands."""

    _service_operations_log: Annotated[
        NotRequired[list[ServiceOperationRecord]],
        PrivateStateAttr,
        _merge_operations_log,
    ]
    """Chronological audit log of operations performed on services in this thread."""

    _plugin_state: Annotated[
        NotRequired[dict[str, Any]],
        PrivateStateAttr,
        _merge_plugin_state,
    ]
    """Extensible namespace for plugins to store private checkpointed state."""

    _active_plugins: Annotated[
        NotRequired[list[str]],
        PrivateStateAttr,
        _merge_unique_strings,
    ]
    """Names of active plugins enabled in the current thread."""


__all__ = [
    "ManagedService",
    "ServiceContextState",
    "ServiceOperationRecord",
    "ServiceOperationType",
    "ServiceStatus",
    "make_operation_record",
    "make_service_record",
]
