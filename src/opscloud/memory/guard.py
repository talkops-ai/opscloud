"""Protect machine-managed memory blocks from agent edits while allowing user memory updates."""

from __future__ import annotations

from opscloud.middleware.memory_guard import (
    ONBOARDING_NAME_MEMORY_END,
    ONBOARDING_NAME_MEMORY_START,
    ManagedMemoryGuardMiddleware,
    extract_onboarding_name_block,
    strip_onboarding_name_markers,
)

__all__ = [
    "ManagedMemoryGuardMiddleware",
    "ONBOARDING_NAME_MEMORY_START",
    "ONBOARDING_NAME_MEMORY_END",
    "extract_onboarding_name_block",
    "strip_onboarding_name_markers",
]
