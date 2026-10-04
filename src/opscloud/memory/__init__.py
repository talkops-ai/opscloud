"""Persistent memory, onboarding wizard, and storage for opscloud."""

from opscloud.memory.branch import BranchMemoryEntry, BranchMemoryStore
from opscloud.memory.guard import ManagedMemoryGuardMiddleware
from opscloud.memory.onboarding import (
    ONBOARDING_NAME_MEMORY_END,
    ONBOARDING_NAME_MEMORY_START,
    extract_onboarding_name_block,
    run_onboarding_if_needed,
    strip_onboarding_name_markers,
)
from opscloud.memory.registry import MemoryRegistry
from opscloud.memory.store import MemoryEntry, MemoryStore

__all__ = [
    "MemoryRegistry",
    "ManagedMemoryGuardMiddleware",
    "MemoryStore",
    "MemoryEntry",
    "BranchMemoryStore",
    "BranchMemoryEntry",
    "run_onboarding_if_needed",
    "extract_onboarding_name_block",
    "strip_onboarding_name_markers",
    "ONBOARDING_NAME_MEMORY_START",
    "ONBOARDING_NAME_MEMORY_END",
]
