"""Unit tests for single authoritative command descriptor and registry parity.

Enforces Section 12.7 and Step 10 of the Command UI Audit:
Ensures 100% parity between COMMAND_DESCRIPTORS and discovered command handlers
across canonical names, aliases, and bypass tiers.
"""

from __future__ import annotations

import inspect
from opscloud.commands import discover_command_handlers
from opscloud.commands._base import BaseCommandHandler
from opscloud.commands._types import BypassTier
from opscloud.ui.command_registry import (
    COMMAND_DESCRIPTORS,
    CommandDescriptor,
    CommandEntry,
    autocomplete_entries,
    command_lookup,
    get_command,
)


def test_command_descriptors_match_discovered_handlers() -> None:
    """Every discovered handler must have an authoritative CommandDescriptor."""
    descriptors = {command.name for command in COMMAND_DESCRIPTORS}
    handlers = {handler.name for handler in discover_command_handlers()}

    assert descriptors == handlers
    assert len(descriptors) == 51


def test_aliases_parity() -> None:
    """Descriptors and discovered handlers must declare identical alias sets."""
    handler_map = {h.name.lower(): h for h in discover_command_handlers()}

    for cmd in COMMAND_DESCRIPTORS:
        handler = handler_map.get(cmd.name.lower())
        assert handler is not None, f"Handler missing for {cmd.name}"
        assert set(cmd.aliases) == set(handler.aliases), (
            f"Alias mismatch for {cmd.name}: "
            f"descriptor has {cmd.aliases}, handler has {handler.aliases}"
        )


def test_bypass_tier_parity() -> None:
    """Descriptors and discovered handlers must declare identical bypass tiers."""
    handler_map = {h.name.lower(): h for h in discover_command_handlers()}

    for cmd in COMMAND_DESCRIPTORS:
        handler = handler_map.get(cmd.name.lower())
        assert handler is not None, f"Handler missing for {cmd.name}"
        assert cmd.bypass_tier == handler.bypass_tier, (
            f"Bypass tier mismatch for {cmd.name}: "
            f"descriptor has {cmd.bypass_tier}, handler has {handler.bypass_tier}"
        )


def test_all_descriptors_have_concrete_handler() -> None:
    """Every CommandDescriptor must specify a concrete BaseCommandHandler subclass."""
    for cmd in COMMAND_DESCRIPTORS:
        assert cmd.handler_type is not None, f"Command {cmd.name} has no handler_type"
        assert issubclass(cmd.handler_type, BaseCommandHandler), (
            f"{cmd.name} handler_type {cmd.handler_type} is not a BaseCommandHandler"
        )
        assert not inspect.isabstract(cmd.handler_type), (
            f"{cmd.name} handler_type {cmd.handler_type} is abstract"
        )


def test_autocomplete_entries_projection() -> None:
    """autocomplete_entries() must project non-prefix commands into CommandEntry instances."""
    entries = autocomplete_entries()

    # Prefix handlers ending with ':' should be excluded from default autocomplete
    assert all(not e.name.endswith(":") for e in entries)
    # Total count should be total descriptors minus prefix handlers (/skill:, /workflow:)
    prefix_count = sum(1 for c in COMMAND_DESCRIPTORS if c.name.endswith(":"))
    assert len(entries) == len(COMMAND_DESCRIPTORS) - prefix_count

    for entry in entries:
        assert isinstance(entry, CommandEntry)
        assert entry.name.startswith("/")
        assert len(entry.description) > 0


def test_command_lookup_mapping() -> None:
    """command_lookup() must map all canonical names and aliases to their descriptors."""
    lookup = command_lookup()

    for cmd in COMMAND_DESCRIPTORS:
        assert lookup.get(cmd.name.lower()) == cmd
        for alias in cmd.aliases:
            assert lookup.get(alias.lower()) == cmd


def test_get_command_resolution() -> None:
    """get_command() must resolve canonical names, aliases, and prefix syntax."""
    # Canonical name
    assert get_command("/model") is not None
    assert get_command("/model").name == "/model"  # type: ignore

    # Case insensitive
    assert get_command("/MODEL") is not None
    assert get_command("/MODEL").name == "/model"  # type: ignore

    # Alias resolution
    assert get_command("/quit") is not None
    assert get_command("/quit").name == "/exit"  # type: ignore

    assert get_command("/aws") is not None
    assert get_command("/aws").name == "/cloud"  # type: ignore

    # Prefix resolution for dynamic skills & workflows
    skill_desc = get_command("/skill:my-custom-skill")
    assert skill_desc is not None
    assert skill_desc.name == "/skill:"

    wf_desc = get_command("/workflow:my-flow")
    assert wf_desc is not None
    assert wf_desc.name == "/workflow:"

    agent_desc = get_command("/agent:my-agent")
    assert agent_desc is not None
    assert agent_desc.name == "/agent:"

    # Unknown command
    assert get_command("/nonexistent_cmd_xyz") is None
