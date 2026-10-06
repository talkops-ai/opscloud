"""Unit tests for Default Marketplace Configurations, Live Discovery, and Dynamic Categories."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from opscloud.config.plugins import (
    DEFAULT_MARKETPLACE_CONFIGS,
    DefaultMarketplaceConfig,
    get_default_marketplace_config,
    is_default_marketplace,
)
from opscloud.plugins.commands_cli import execute_plugin_command
from opscloud.plugins.discovery import _normalize_plugin_id
from opscloud.plugins.marketplace import (
    _parse_entry,
    load_live_default_marketplace,
    materialize_plugin_source,
)
from opscloud.plugins.models import (
    LocalPluginSource,
    MarketplacePluginEntry,
    PluginMarketplace,
)
from opscloud.plugins.store import (
    load_marketplace_records,
    remove_marketplace_record,
    save_marketplace_record,
)


def _sample_marketplace_payload() -> dict[str, object]:
    return {
        "name": "talkops-devops-plugins",
        "owner": {"name": "TalkOps AI", "url": "https://github.com/talkops-ai"},
        "metadata": {
            "description": "DevOps and cloud-operations plugins for AI agents.",
            "version": "0.1.0",
        },
        "plugins": [
            {
                "name": "aws-iac-engineer",
                "displayName": "AWS IaC Engineer",
                "source": "./plugins/agent-plugins/aws-iac-engineer",
                "description": "Authors, validates, deploys, and troubleshoots AWS infrastructure-as-code.",
                "version": "0.1.0",
                "category": "infrastructure-as-code",
                "keywords": ["aws", "iac", "cdk", "cloudformation"],
            },
            {
                "name": "aws-finops-agent",
                "displayName": "AWS FinOps Agent",
                "source": "./plugins/agent-plugins/aws-finops-agent",
                "description": "Cloud cost optimization and rightsizing specialist.",
                "version": "0.1.0",
                "category": "finops",
                "keywords": ["aws", "finops", "cost", "savings"],
            },
            {
                "name": "aws-networking",
                "displayName": "AWS Networking",
                "source": "./plugins/vertical-plugins/aws-networking",
                "description": "VPC, Transit Gateway, Route53, and load balancing skills.",
                "version": "0.1.0",
                "category": "networking",
                "keywords": ["aws", "vpc", "tgw", "networking"],
            },
            {
                "name": "terraform",
                "displayName": "Terraform (HashiCorp)",
                "source": "./plugins/partner-built/terraform",
                "description": "Official HashiCorp Terraform skills and integrations.",
                "version": "1.0.0",
                "category": "partner-integrations",
                "keywords": ["terraform", "hcl", "hashicorp"],
            },
        ],
    }


def test_default_marketplace_configs():
    """Verify default marketplace config list and helper resolution."""
    assert len(DEFAULT_MARKETPLACE_CONFIGS) >= 1
    primary = DEFAULT_MARKETPLACE_CONFIGS[0]
    assert primary.name == "talkops-devops-plugins"
    assert "github.com/talkops-ai/devops-plugins" in primary.repository_url
    assert "marketplace.json" in primary.manifest_url
    assert primary.ref == "main"
    assert is_default_marketplace("talkops-devops-plugins") is True
    assert is_default_marketplace("nonexistent-marketplace") is False
    assert get_default_marketplace_config("talkops-devops-plugins") == primary
    assert get_default_marketplace_config("nonexistent") is None


def test_default_marketplace_config_from_github():
    """Verify from_github factory creates valid configurations generically."""
    cfg = DefaultMarketplaceConfig.from_github(
        "my-org/cloud-plugins",
        ref="release-v1",
        description="My Cloud Plugins",
    )
    assert cfg.name == "my-org-cloud-plugins"
    assert cfg.repository_url == "https://github.com/my-org/cloud-plugins.git"
    assert (
        cfg.manifest_url
        == "https://raw.githubusercontent.com/my-org/cloud-plugins/release-v1/.claude-plugin/marketplace.json"
    )
    assert cfg.ref == "release-v1"
    assert cfg.description == "My Cloud Plugins"

    # With full git url and explicit name
    cfg2 = DefaultMarketplaceConfig.from_github(
        "https://github.com/talkops-ai/devops-plugins.git",
        name="talkops-devops-plugins",
    )
    assert cfg2.name == "talkops-devops-plugins"
    assert cfg2.repository_url == "https://github.com/talkops-ai/devops-plugins.git"


def test_load_marketplace_records_seeds_default_marketplaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify that all configured default marketplaces are seeded automatically."""
    state_file = tmp_path / "plugin_marketplaces.json"
    monkeypatch.setattr("opscloud.plugins.store._marketplaces_path", lambda: state_file)

    records = load_marketplace_records()
    for cfg in DEFAULT_MARKETPLACE_CONFIGS:
        assert cfg.name in records
        rec = records[cfg.name]
        assert rec.name == cfg.name
        assert rec.source == cfg.repository_url
        assert rec.source_type == "git"
        assert rec.is_default is True


def test_default_marketplace_removal_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify that explicitly removing a default marketplace persists and prevents re-seeding."""
    state_file = tmp_path / "plugin_marketplaces.json"
    monkeypatch.setattr("opscloud.plugins.store._marketplaces_path", lambda: state_file)

    # First load seeds all defaults
    records = load_marketplace_records()
    target_name = DEFAULT_MARKETPLACE_CONFIGS[0].name
    assert target_name in records

    # Save to file
    save_marketplace_record(records[target_name])

    # User removes it
    removed = remove_marketplace_record(target_name)
    assert removed is True

    # Next load does NOT re-seed it
    records_after = load_marketplace_records()
    assert target_name not in records_after


def test_marketplace_plugin_entry_generic_category_label():
    """Verify generic category_label formatting without hardcoded string parsing."""
    e1 = MarketplacePluginEntry(
        name="p1",
        source=LocalPluginSource(source_type="local", path="./plugins/some-path/p1"),
        category="infrastructure-as-code",
    )
    assert e1.category_label == "Infrastructure As Code"

    e2 = MarketplacePluginEntry(
        name="p2",
        source=LocalPluginSource(source_type="local", path="./plugins/any-path/p2"),
        category="ai-agents",
    )
    assert e2.category_label == "Ai Agents"

    e3 = MarketplacePluginEntry(
        name="p3",
        source=LocalPluginSource(source_type="local", path="./plugins/p3"),
        category=None,
    )
    assert e3.category_label == "Plugins"


def test_parse_entry_extracts_version_category_keywords():
    """Verify that _parse_entry preserves version, category, and keywords."""
    raw = {
        "name": "aws-sre-agent",
        "displayName": "AWS SRE Agent",
        "source": "./plugins/agent-plugins/aws-sre-agent",
        "description": "Site reliability operations",
        "version": "0.2.5",
        "category": "observability-agents",
        "keywords": ["aws", "cloudwatch", "alarms"],
    }
    warnings: list[str] = []
    entry = _parse_entry(raw, warnings=warnings)
    assert entry is not None
    assert entry.name == "aws-sre-agent"
    assert entry.display_name == "AWS SRE Agent"
    assert entry.version == "0.2.5"
    assert entry.category == "observability-agents"
    assert entry.category_label == "Observability Agents"
    assert entry.keywords == ("aws", "cloudwatch", "alarms")


def test_shorthand_plugin_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify shorthand resolution resolves across available default marketplaces."""
    default_name = DEFAULT_MARKETPLACE_CONFIGS[0].name

    # When no '@' is present, defaults to primary default marketplace
    normalized = _normalize_plugin_id("aws-finops-agent")
    assert normalized == f"aws-finops-agent@{default_name}"

    # Explicit '@' is preserved
    explicit = _normalize_plugin_id("custom-plugin@my-marketplace")
    assert explicit == "custom-plugin@my-marketplace"


def test_load_live_default_marketplace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify load_live_default_marketplace fetches and parses the live catalog."""
    mock_payload = _sample_marketplace_payload()
    monkeypatch.setattr(
        "opscloud.config.plugins.fetch_marketplace_manifest",
        lambda url, **kwargs: mock_payload,
    )

    default_root = tmp_path / "talkops-devops-plugins"
    mp = load_live_default_marketplace(default_root)
    assert mp.name == "talkops-devops-plugins"
    assert len(mp.plugins) == 4
    names = [p.name for p in mp.plugins]
    assert "aws-iac-engineer" in names
    assert "aws-finops-agent" in names
    assert "aws-networking" in names
    assert "terraform" in names


def test_cli_plugin_list_dynamic_categories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify opscloud plugin list dynamically groups available plugins by category."""
    mock_payload = _sample_marketplace_payload()
    monkeypatch.setattr(
        "opscloud.config.plugins.fetch_marketplace_manifest",
        lambda url, **kwargs: mock_payload,
    )
    state_file = tmp_path / "plugin_marketplaces.json"
    monkeypatch.setattr("opscloud.plugins.store._marketplaces_path", lambda: state_file)
    monkeypatch.setattr("opscloud.plugins.store.load_installed_plugins", lambda **kwargs: {})
    monkeypatch.setattr("opscloud.plugins.commands_cli._resolve_project_root", lambda scope: None)

    args = argparse.Namespace(plugin_command="list", output_format="text")
    output = execute_plugin_command(args)
    assert output is not None

    assert "Installed Plugins:" in output
    assert "(none installed)" in output
    assert "Available in talkops-devops-plugins (Default Marketplace):" in output
    assert "Infrastructure As Code:" in output
    assert "aws-iac-engineer" in output
    assert "Finops:" in output
    assert "aws-finops-agent" in output
    assert "Networking:" in output
    assert "aws-networking" in output
    assert "Partner Integrations:" in output
    assert "terraform" in output


def test_cli_plugin_search(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify opscloud plugin search query matching and JSON output."""
    mock_payload = _sample_marketplace_payload()
    monkeypatch.setattr(
        "opscloud.config.plugins.fetch_marketplace_manifest",
        lambda url, **kwargs: mock_payload,
    )
    state_file = tmp_path / "plugin_marketplaces.json"
    monkeypatch.setattr("opscloud.plugins.store._marketplaces_path", lambda: state_file)
    monkeypatch.setattr("opscloud.plugins.store.load_installed_plugins", lambda **kwargs: {})
    monkeypatch.setattr("opscloud.plugins.commands_cli._resolve_project_root", lambda scope: None)

    # Search by keyword "finops"
    args = argparse.Namespace(plugin_command="search", query="finops", output_format="text")
    output = execute_plugin_command(args)
    assert output is not None
    assert "Found 1 plugin(s) matching 'finops':" in output
    assert "AWS FinOps Agent" in output
    assert "Finops" in output

    # Search by "terraform"
    args_tf = argparse.Namespace(plugin_command="search", query="terraform", output_format="text")
    out_tf = execute_plugin_command(args_tf)
    assert out_tf is not None
    assert "Terraform (HashiCorp)" in out_tf
    assert "Partner Integrations" in out_tf

    # Non-matching search
    args_none = argparse.Namespace(plugin_command="search", query="nonexistent_xyz", output_format="text")
    out_none = execute_plugin_command(args_none)
    assert "No plugins found matching 'nonexistent_xyz'." in (out_none or "")

    # JSON search output
    args_json = argparse.Namespace(plugin_command="search", query="networking", output_format="json")
    out_json = execute_plugin_command(args_json)
    assert out_json is None  # Printed to stdout directly


def test_materialize_plugin_source_triggers_on_demand_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify materialize_plugin_source clones the default marketplace repo on demand if files are missing."""
    cloned_paths: list[Path] = []

    def mock_clone(source, git_url, *, cache_key, destination=None, validate=None):
        target = destination or tmp_path
        target.mkdir(parents=True, exist_ok=True)
        plugin_dir = target / "plugins" / "agent-plugins" / "aws-finops-agent"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        (plugin_dir / "plugin.json").write_text('{"name": "aws-finops-agent"}', encoding="utf-8")
        cloned_paths.append(target)
        return target

    monkeypatch.setattr("opscloud.plugins.marketplace._clone_repository_to_cache", mock_clone)

    primary_name = DEFAULT_MARKETPLACE_CONFIGS[0].name
    mp = PluginMarketplace(
        name=primary_name,
        root=tmp_path / "devops_mp_root",
        manifest_path=tmp_path / "devops_mp_root" / "marketplace.json",
        metadata={},
        plugins=(),
    )
    entry = MarketplacePluginEntry(
        name="aws-finops-agent",
        source=LocalPluginSource(source_type="local", path="./plugins/agent-plugins/aws-finops-agent"),
    )

    resolved = materialize_plugin_source(mp, entry)
    assert resolved is not None
    assert resolved.exists()
    assert len(cloned_paths) == 1
    assert cloned_paths[0] == mp.root


def test_refresh_marketplace_updates_catalog_and_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify refresh_marketplace fetches latest catalog and updates state record."""
    from opscloud.plugins.discovery import refresh_marketplace

    primary_name = DEFAULT_MARKETPLACE_CONFIGS[0].name
    mock_payload = _sample_marketplace_payload()
    monkeypatch.setattr(
        "opscloud.config.plugins.fetch_marketplace_manifest",
        lambda url, **kwargs: mock_payload,
    )
    state_file = tmp_path / "plugin_marketplaces.json"
    monkeypatch.setattr("opscloud.plugins.store._marketplaces_path", lambda: state_file)

    # Initial seeding
    records = load_marketplace_records()
    assert primary_name in records

    # Refresh
    mp = refresh_marketplace(primary_name)
    assert mp.name == primary_name
    assert len(mp.plugins) == 4

    # Verify record was updated
    records_after = load_marketplace_records()
    assert records_after[primary_name].plugin_count == 4


def test_cli_plugin_refresh_and_marketplace_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify opscloud plugin refresh and opscloud plugin marketplace refresh."""
    primary_name = DEFAULT_MARKETPLACE_CONFIGS[0].name
    mock_payload = _sample_marketplace_payload()
    monkeypatch.setattr(
        "opscloud.config.plugins.fetch_marketplace_manifest",
        lambda url, **kwargs: mock_payload,
    )
    state_file = tmp_path / "plugin_marketplaces.json"
    monkeypatch.setattr("opscloud.plugins.store._marketplaces_path", lambda: state_file)

    # Test opscloud plugin refresh <name>
    args_one = argparse.Namespace(
        plugin_command="refresh", name=primary_name, output_format="text"
    )
    out_one = execute_plugin_command(args_one)
    assert out_one is not None
    assert f"Refreshed marketplace '{primary_name}'" in out_one

    # Test opscloud plugin refresh (all)
    args_all = argparse.Namespace(
        plugin_command="refresh", name=None, output_format="text"
    )
    out_all = execute_plugin_command(args_all)
    assert out_all is not None
    assert "Refreshed 1 marketplace(s):" in out_all

    # Test opscloud plugin marketplace refresh (subcommand)
    args_sub = argparse.Namespace(
        plugin_command="marketplace",
        marketplace_command="refresh",
        name=primary_name,
        output_format="text",
    )
    out_sub = execute_plugin_command(args_sub)
    assert out_sub is not None
    assert f"Refreshed marketplace '{primary_name}'" in out_sub

    # Test JSON output format
    args_json = argparse.Namespace(
        plugin_command="refresh", name=primary_name, output_format="json"
    )
    out_json = execute_plugin_command(args_json)
    assert out_json is None  # Printed to stdout directly


def test_tui_marketplace_details_contains_refresh_option():
    """Verify TUI marketplace details view presents 'Refresh marketplace' option."""
    from opscloud.ui.widgets.plugin_manager import _marketplace_details_options

    options = _marketplace_details_options()
    option_ids = [opt.id for opt in options]
    assert "action:refresh-marketplace" in option_ids
    assert "action:remove-marketplace" in option_ids
    assert "details-back" in option_ids
    assert option_ids[0] == "action:refresh-marketplace"
