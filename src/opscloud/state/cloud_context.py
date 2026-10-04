"""Cloud context, environment, and resource tracking state channels for OpsCloud.

Provides strongly typed cloud provider context (AWS, Azure, GCP, Kubernetes,
Terraform/IaC), safety execution flags (dry-run, production environment), and
an accumulating audit of cloud resources touched during an operational session.
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

CloudProvider = Literal[
    "aws",
    "azure",
    "gcp",
    "kubernetes",
    "terraform",
    "multi",
    "local",
]
"""Supported cloud platforms and operational domains."""

_CLOUD_PROVIDERS: frozenset[str] = frozenset(get_args(CloudProvider))

CloudResourceAction = Literal["inspected", "created", "modified", "deleted"]
"""Action taken on a cloud resource."""


class AWSContext(TypedDict, total=False):
    """Active AWS session identity and region."""

    profile: str | None
    region: str | None
    account_id: str | None
    role_arn: str | None


class AzureContext(TypedDict, total=False):
    """Active Azure subscription and resource group context."""

    subscription_id: str | None
    tenant_id: str | None
    resource_group: str | None
    location: str | None


class GCPContext(TypedDict, total=False):
    """Active Google Cloud project and zone context."""

    project_id: str | None
    region: str | None
    zone: str | None


class K8sContext(TypedDict, total=False):
    """Active Kubernetes cluster and namespace context."""

    current_context: str | None
    cluster_name: str | None
    namespace: str | None


class IaCContext(TypedDict, total=False):
    """Active Infrastructure-as-Code workspace context."""

    tool: Literal["terraform", "opentofu", "pulumi", "terragrunt"]
    workspace: str | None
    working_dir: str | None


class CloudEnvironmentContext(TypedDict, total=False):
    """Composite container holding active cloud provider details."""

    active_provider: CloudProvider
    aws: AWSContext
    azure: AzureContext
    gcp: GCPContext
    k8s: K8sContext
    iac: IaCContext
    is_production: bool
    notes: str | None


class CloudResourceRef(TypedDict):
    """Audit record for a cloud resource inspected or modified during the session."""

    provider: str
    resource_type: str
    resource_id: str
    action: CloudResourceAction
    timestamp: str


def _merge_cloud_context(
    existing: CloudEnvironmentContext | None,
    new: CloudEnvironmentContext | None,
) -> CloudEnvironmentContext | None:
    """Reducer merging two cloud context snapshots."""
    if not existing:
        return new
    if not new:
        return existing
    merged: dict[str, Any] = dict(existing)
    for key, value in new.items():
        existing_val = merged.get(key)
        if isinstance(value, dict) and isinstance(existing_val, dict):
            merged[key] = {**existing_val, **value}
        else:
            merged[key] = value
    return cast(CloudEnvironmentContext, merged)


def _merge_cloud_resources(
    existing: list[CloudResourceRef] | None,
    new: list[CloudResourceRef] | None,
) -> list[CloudResourceRef]:
    """Accumulating reducer for touched cloud resources preserving order and unique entries."""
    if not existing:
        return list(new or [])
    if not new:
        return list(existing)

    seen: set[tuple[str, str, str, str]] = {
        (r["provider"], r["resource_type"], r["resource_id"], r["action"])
        for r in existing
    }
    result = list(existing)
    for ref in new:
        key = (ref["provider"], ref["resource_type"], ref["resource_id"], ref["action"])
        if key not in seen:
            seen.add(key)
            result.append(ref)
    return result


def coerce_cloud_provider(value: object) -> CloudProvider | None:
    """Narrow an untrusted value to a valid CloudProvider."""
    if isinstance(value, str) and value in _CLOUD_PROVIDERS:
        return cast(CloudProvider, value)
    return None


def make_resource_ref(
    provider: str,
    resource_type: str,
    resource_id: str,
    action: CloudResourceAction,
) -> CloudResourceRef:
    """Convenience factory for constructing a CloudResourceRef."""
    return {
        "provider": provider,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "action": action,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def format_cloud_badge(cloud_ctx: CloudEnvironmentContext | None) -> str:
    """Format a compact string badge representing active cloud context (for TUI/CLI)."""
    if not cloud_ctx:
        return ""
    provider = cloud_ctx.get("active_provider")
    if not provider:
        return ""

    if provider == "aws":
        aws = cloud_ctx.get("aws", {})
        region = aws.get("region") or ""
        profile = aws.get("profile") or ""
        return f"[AWS {profile or region or 'default'}]"
    if provider == "kubernetes":
        k8s = cloud_ctx.get("k8s", {})
        ctx = k8s.get("current_context") or "default"
        ns = k8s.get("namespace")
        return f"[K8s {ctx}/{ns}]" if ns else f"[K8s {ctx}]"
    if provider == "azure":
        az = cloud_ctx.get("azure", {})
        rg = az.get("resource_group") or ""
        return f"[Azure {rg}]" if rg else "[Azure]"
    if provider == "gcp":
        gcp = cloud_ctx.get("gcp", {})
        proj = gcp.get("project_id") or ""
        return f"[GCP {proj}]" if proj else "[GCP]"
    if provider == "terraform":
        iac = cloud_ctx.get("iac", {})
        ws = iac.get("workspace") or "default"
        return f"[TF {ws}]"
    return f"[{provider.upper()}]"


class CloudContextState(AgentState):
    """State channels for cloud operations context and execution boundaries."""

    _active_cloud_provider: Annotated[
        NotRequired[CloudProvider | None],
        PrivateStateAttr,
    ]
    """Current primary cloud provider (aws, azure, gcp, kubernetes, terraform, etc.)."""

    _cloud_context: Annotated[
        NotRequired[CloudEnvironmentContext | None],
        PrivateStateAttr,
        _merge_cloud_context,
    ]
    """Structured environment details (profiles, accounts, subscriptions, regions, clusters)."""

    _cloud_resources_touched: Annotated[
        NotRequired[list[CloudResourceRef]],
        PrivateStateAttr,
        _merge_cloud_resources,
    ]
    """Accumulating audit trail of all cloud resources inspected, created, modified, or deleted."""

    _dry_run: Annotated[
        NotRequired[bool],
        PrivateStateAttr,
    ]
    """Whether cloud operations should run in simulation / plan / dry-run mode."""

    _is_production_environment: Annotated[
        NotRequired[bool],
        PrivateStateAttr,
    ]
    """Whether the active target environment is production or mission-critical."""

    _environment_fingerprint: Annotated[
        NotRequired[str | None],
        PrivateStateAttr,
    ]
    """Fingerprint of available CLI binaries (aws, az, gcloud, kubectl, terraform) and credentials."""


__all__ = [
    "AWSContext",
    "AzureContext",
    "CloudContextState",
    "CloudEnvironmentContext",
    "CloudProvider",
    "CloudResourceAction",
    "CloudResourceRef",
    "GCPContext",
    "IaCContext",
    "K8sContext",
    "coerce_cloud_provider",
    "format_cloud_badge",
    "make_resource_ref",
]
