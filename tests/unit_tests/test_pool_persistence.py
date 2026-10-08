"""Unit tests for agent model pool persistence, context switches, and restart behavior."""

from __future__ import annotations

from pathlib import Path
import pytest
from opscloud.commands._base import CommandContext
from opscloud.commands.core.pool import PoolHandler
from opscloud.config.toml_config import (
    clear_agent_pool,
    load_agent_pool,
    read_config_toml,
    save_agent_pool,
)
from opscloud.model.pool import DynamicModelPoolManager, get_model_pool_manager


def test_pool_save_and_load_persistence(tmp_path: Path):
    """Test saving and loading single-provider and multi-provider pools from config.toml."""
    config_file = tmp_path / "custom_config.toml"

    # Initially empty
    assert load_agent_pool(config_file) is None

    # Save single-provider pool
    single_pool = {
        "fast": "openai:gpt-5.4-mini",
        "standard": "openai:gpt-5.6-luna",
        "powerful": "openai:gpt-6-astra",
    }
    assert save_agent_pool(single_pool, provider="openai", config_path=config_file) is True

    loaded = load_agent_pool(config_file)
    assert loaded is not None
    assert loaded["fast"] == "openai:gpt-5.4-mini"
    assert loaded["standard"] == "openai:gpt-5.6-luna"
    assert loaded["powerful"] == "openai:gpt-6-astra"
    assert loaded["provider"] == "openai"

    raw_toml = read_config_toml(config_file)
    assert "agent_pool" in raw_toml
    assert raw_toml["agent_pool"]["fast"] == "openai:gpt-5.4-mini"


def test_pool_multi_provider_persistence(tmp_path: Path):
    """Test multi-provider pool configuration persistence."""
    config_file = tmp_path / "multi_config.toml"

    multi_pool = {
        "fast": "openai:gpt-5.4-mini",
        "standard": "google_genai:gemini-2.5-flash",
        "powerful": "anthropic:claude-3-5-sonnet",
    }
    assert save_agent_pool(multi_pool, provider="multi", config_path=config_file) is True

    loaded = load_agent_pool(config_file)
    assert loaded is not None
    assert loaded["fast"] == "openai:gpt-5.4-mini"
    assert loaded["standard"] == "google_genai:gemini-2.5-flash"
    assert loaded["powerful"] == "anthropic:claude-3-5-sonnet"
    assert loaded["provider"] == "multi"


def test_discover_tiers_honors_saved_pool_across_contexts():
    """Verify discover_tiers returns the saved pool regardless of active context base_spec."""
    pool_mgr = DynamicModelPoolManager()

    # Save custom pool
    custom_pool = {
        "fast": "openai:gpt-5.4-mini",
        "standard": "openai:gpt-5.6-luna",
        "powerful": "openai:gpt-6-astra",
    }
    save_agent_pool(custom_pool, provider="openai")

    # 1. No provider or base_spec (standard /pool status or default invoke)
    tiers = pool_mgr.discover_tiers()
    assert tiers[0][0] == "openai:gpt-5.4-mini"
    assert tiers[1][0] == "openai:gpt-5.6-luna"
    assert tiers[2][0] == "openai:gpt-6-astra"

    # 2. Context switched to a Google Gemini model
    tiers_gemini = pool_mgr.discover_tiers(base_spec="google_genai:gemini-2.5-flash")
    assert tiers_gemini[0][0] == "openai:gpt-5.4-mini"
    assert tiers_gemini[1][0] == "openai:gpt-5.6-luna"
    assert tiers_gemini[2][0] == "openai:gpt-6-astra"

    # 3. Context switched to an Anthropic model
    tiers_claude = pool_mgr.discover_tiers(base_spec="anthropic:claude-sonnet-4-5")
    assert tiers_claude[0][0] == "openai:gpt-5.4-mini"
    assert tiers_claude[1][0] == "openai:gpt-5.6-luna"
    assert tiers_claude[2][0] == "openai:gpt-6-astra"

    # 4. Explicit provider query for matching provider
    tiers_openai = pool_mgr.discover_tiers(provider="openai")
    assert tiers_openai[0][0] == "openai:gpt-5.4-mini"

    # 5. Explicit foreign provider query (e.g. dropdown picker for another provider)
    # should fall through to registry discovery for that provider
    tiers_foreign = pool_mgr.discover_tiers(provider="anthropic")
    assert "anthropic" in tiers_foreign[0][0]

    # 6. ignore_custom_pool=True forces dynamic discovery
    tiers_raw = pool_mgr.discover_tiers(provider="openai", ignore_custom_pool=True)
    assert 0 in tiers_raw


def test_discover_tiers_multi_provider_pool():
    """Verify multi-provider pool is preserved and returned across contexts."""
    pool_mgr = DynamicModelPoolManager()

    multi_pool = {
        "fast": "openai:gpt-5.4-mini",
        "standard": "google_genai:gemini-2.5-flash",
        "powerful": "anthropic:claude-3-5-sonnet",
    }
    save_agent_pool(multi_pool, provider="multi")

    # Routing call with no provider specified
    tiers = pool_mgr.discover_tiers()
    assert tiers[0][0] == "openai:gpt-5.4-mini"
    assert tiers[1][0] == "google_genai:gemini-2.5-flash"
    assert tiers[2][0] == "anthropic:claude-3-5-sonnet"

    # Routing call with ambient base_spec
    tiers_ambient = pool_mgr.discover_tiers(base_spec="google_genai:gemini-2.5-pro")
    assert tiers_ambient[0][0] == "openai:gpt-5.4-mini"
    assert tiers_ambient[1][0] == "google_genai:gemini-2.5-flash"
    assert tiers_ambient[2][0] == "anthropic:claude-3-5-sonnet"


@pytest.mark.asyncio
async def test_pool_cli_handler_lifecycle():
    """Test /pool set, status, and clear CLI execution lifecycle."""
    handler = PoolHandler()
    mgr = get_model_pool_manager()

    # 1. Set custom pool
    ctx_set = CommandContext(
        app=None,
        raw_command="/pool set fast=openai:gpt-5.4-mini standard=openai:gpt-5.6-luna powerful=openai:gpt-6-astra",
        args="set fast=openai:gpt-5.4-mini standard=openai:gpt-5.6-luna powerful=openai:gpt-6-astra",
    )
    res_set = await handler.execute(ctx_set)
    assert res_set.success is True
    assert "Agent Model Pool Updated" in (res_set.message or "")

    persisted = load_agent_pool()
    assert persisted is not None
    assert persisted["fast"] == "openai:gpt-5.4-mini"

    # 2. Check status reflects persisted pool
    ctx_status = CommandContext(app=None, raw_command="/pool status", args="status")
    res_status = await handler.execute(ctx_status)
    assert res_status.success is True
    assert "Custom (`config.toml [agent_pool]`)" in (res_status.message or "")
    assert "openai:gpt-5.4-mini" in (res_status.message or "")

    # 3. Simulate process restart: fresh manager instance reads persisted config
    fresh_mgr = DynamicModelPoolManager()
    fresh_tiers = fresh_mgr.discover_tiers()
    assert fresh_tiers[0][0] == "openai:gpt-5.4-mini"
    assert fresh_tiers[1][0] == "openai:gpt-5.6-luna"
    assert fresh_tiers[2][0] == "openai:gpt-6-astra"

    # 4. Clear pool reverts to dynamic discovery
    ctx_clear = CommandContext(app=None, raw_command="/pool clear", args="clear")
    res_clear = await handler.execute(ctx_clear)
    assert res_clear.success is True
    assert load_agent_pool() is None

    cleared_tiers = fresh_mgr.discover_tiers()
    assert 0 in cleared_tiers
