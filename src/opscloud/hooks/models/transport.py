"""Versioned hook invocation transport models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from opscloud.hooks.models.domain import HookDecision, HookInvocation


class _TransportModel(BaseModel):
    """Base model for hook transport payloads with wire-format helpers."""

    model_config = ConfigDict(extra="forbid")


class HookInvocationRequest(_TransportModel):
    """Request sent for a server-owned hook invocation."""

    protocol_version: Literal[1]
    invocation_id: UUID
    snapshot_id: str
    run_id: str
    invocation: HookInvocation
    deadline: datetime


class HookInvocationResponse(_TransportModel):
    """Response returned for a server-owned hook invocation."""

    protocol_version: Literal[1]
    invocation_id: UUID
    snapshot_id: str
    decision: HookDecision
