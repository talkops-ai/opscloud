"""Unit tests for plugin scoping across agent-plugins, partner-built, and vertical-plugins.

Verifies:
1. Agent plugins (with agents/ folder) spawn dynamic subagents; their skills & MCP servers are isolated to the subagent.
2. Partner-built plugins (without agents/ folder, with .mcp.json & skills/) attach skills & MCP servers directly to the main coordinator agent.
3. Vertical plugins (without agents/ folder, without .mcp.json, with skills/) attach skills directly to the main coordinator agent.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from opscloud.agent.factory import create_opscloud_agent
from opscloud.mcp.discovery import MCPDiscovery
from opscloud.middleware.skills import PluginSkillsMiddleware
from opscloud.skills.registry import SkillRegistry


@pytest.fixture(autouse=True)
def reset_registries():
    SkillRegistry.reset()
    yield
    SkillRegistry.reset()


def _create_agent_plugin(plugins_dir: Path, name: str = "earnings-reviewer") -> Path:
    """Create a plugin with agents/, skills/, and .mcp.json."""
    plugin_dir = plugins_dir / name
    (plugin_dir / "agents").mkdir(parents=True, exist_ok=True)
    (plugin_dir / "skills" / "earnings-analysis").mkdir(parents=True, exist_ok=True)

    (plugin_dir / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.0.0", "description": f"{name} agent plugin"}),
        encoding="utf-8",
    )
    (plugin_dir / "agents" / f"{name}.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: Autonomous {name} specialist\n"
        "tools: execute\n"
        "---\n"
        f"You are the {name} subagent.\n",
        encoding="utf-8",
    )
    (plugin_dir / "skills" / "earnings-analysis" / "SKILL.md").write_text(
        "---\n"
        "name: earnings-analysis\n"
        "description: Analyze financial earnings reports\n"
        "---\n"
        "# Earnings Analysis Guide\n",
        encoding="utf-8",
    )
    (plugin_dir / ".mcp.json").write_text(
        json.dumps({
            "mcpServers": {
                f"{name}-mcp": {
                    "type": "stdio",
                    "command": f"{name}-server",
                    "args": [],
                }
            }
        }),
        encoding="utf-8",
    )
    return plugin_dir


def _create_partner_plugin(plugins_dir: Path, name: str = "lseg") -> Path:
    """Create a partner-built plugin with skills/ and .mcp.json (NO agents/ folder)."""
    plugin_dir = plugins_dir / name
    (plugin_dir / "skills" / "lseg-data").mkdir(parents=True, exist_ok=True)

    (plugin_dir / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.0.0", "description": f"{name} partner connector"}),
        encoding="utf-8",
    )
    (plugin_dir / "skills" / "lseg-data" / "SKILL.md").write_text(
        "---\n"
        "name: lseg-data\n"
        "description: Query LSEG market datasets\n"
        "---\n"
        "# LSEG Data Queries\n",
        encoding="utf-8",
    )
    (plugin_dir / ".mcp.json").write_text(
        json.dumps({
            "mcpServers": {
                "lseg-mcp": {
                    "type": "http",
                    "url": "https://api.analytics.lseg.com/mcp",
                }
            }
        }),
        encoding="utf-8",
    )
    return plugin_dir


def _create_vertical_plugin(plugins_dir: Path, name: str = "equity-research") -> Path:
    """Create a vertical plugin with skills/ only (NO agents/ folder, NO .mcp.json)."""
    plugin_dir = plugins_dir / name
    (plugin_dir / "skills" / "dcf-modeling").mkdir(parents=True, exist_ok=True)

    (plugin_dir / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.0.0", "description": f"{name} vertical workflow"}),
        encoding="utf-8",
    )
    (plugin_dir / "skills" / "dcf-modeling" / "SKILL.md").write_text(
        "---\n"
        "name: dcf-modeling\n"
        "description: Build discounted cash flow valuation models\n"
        "---\n"
        "# DCF Modeling Instructions\n",
        encoding="utf-8",
    )
    return plugin_dir


def test_mcp_discovery_scopes_plugin_mcp(tmp_path: Path) -> None:
    """Verify MCP discovery includes partner plugin MCP and excludes agent plugin MCP."""
    plugins_dir = tmp_path / "plugins"
    _create_agent_plugin(plugins_dir, "earnings-reviewer")
    _create_partner_plugin(plugins_dir, "lseg")
    _create_vertical_plugin(plugins_dir, "equity-research")

    discovery = MCPDiscovery()
    configs = discovery.discover(project_root=tmp_path)

    # Partner plugin MCP should be discovered globally for the coordinator
    assert "lseg-mcp" in configs
    assert configs["lseg-mcp"].get("url") == "https://api.analytics.lseg.com/mcp"

    # Agent plugin MCP should NOT be discovered globally (isolated to subagent)
    assert "earnings-reviewer-mcp" not in configs


def test_skill_registry_scopes_plugin_skills(tmp_path: Path) -> None:
    """Verify SkillRegistry includes partner and vertical plugin skills, and isolates agent plugin skills."""
    plugins_dir = tmp_path / "plugins"
    _create_agent_plugin(plugins_dir, "earnings-reviewer")
    _create_partner_plugin(plugins_dir, "lseg")
    _create_vertical_plugin(plugins_dir, "equity-research")

    registry = SkillRegistry.get_instance()
    sources = registry.get_sources_for_middleware(project_root=tmp_path)
    source_paths = [s[0] for s in sources]

    # Partner plugin skills attached to coordinator
    assert any("lseg" in sp for sp in source_paths)

    # Vertical plugin skills attached to coordinator
    assert any("equity-research" in sp for sp in source_paths)

    # Agent plugin skills excluded from coordinator (isolated to subagent)
    assert not any("earnings-reviewer" in sp for sp in source_paths)


def test_skill_registry_includes_agent_plugin_skills_when_requested(tmp_path: Path) -> None:
    """Verify SkillRegistry includes agent plugin skills when include_subagent_skills is True."""
    plugins_dir = tmp_path / "plugins"
    _create_agent_plugin(plugins_dir, "earnings-reviewer")
    _create_partner_plugin(plugins_dir, "lseg")
    _create_vertical_plugin(plugins_dir, "equity-research")

    registry = SkillRegistry.get_instance()
    sources = registry.get_sources_for_middleware(project_root=tmp_path, include_subagent_skills=True)
    source_paths = [s[0] for s in sources]

    # When requested (e.g. for goal criteria planning), all are included
    assert any("lseg" in sp for sp in source_paths)
    assert any("equity-research" in sp for sp in source_paths)
    assert any("earnings-reviewer" in sp for sp in source_paths)


@pytest.mark.asyncio
async def test_agent_factory_dynamic_subagents_and_coordinator_attachment(tmp_path: Path) -> None:
    """Verify agent factory compiles dynamic subagents for agent plugins and isolates their skills."""
    plugins_dir = tmp_path / "plugins"
    _create_agent_plugin(plugins_dir, "earnings-reviewer")
    _create_partner_plugin(plugins_dir, "lseg")
    _create_vertical_plugin(plugins_dir, "equity-research")

    from langchain_core.language_models.chat_models import BaseChatModel

    mock_model = MagicMock(spec=BaseChatModel)
    mock_model.bind_tools = MagicMock(return_value=mock_model)
    mock_model.profile = {}

    with patch("opscloud.agent.factory.create_deep_agent") as mock_create:
        mock_create.return_value = MagicMock()

        agent_graph, backend = create_opscloud_agent(
            model=mock_model,
            cwd=tmp_path,
            auto_approve=True,
            interactive=False,
        )

        assert mock_create.called
        call_kwargs = mock_create.call_args.kwargs

        # Check compiled subagents includes dynamic subagent from agent plugin
        subagents = call_kwargs.get("subagents", [])
        subagent_names = {s["name"] for s in subagents if isinstance(s, dict)}

        assert "earnings-reviewer" in subagent_names or any(
            "earnings-reviewer" in name for name in subagent_names
        )

        # Dynamic subagent has its own PluginSkillsMiddleware containing its skills
        matching_sub = [s for s in subagents if isinstance(s, dict) and "earnings-reviewer" in s["name"]][0]
        mw_list = matching_sub.get("middleware", [])
        skills_mws = [mw for mw in mw_list if isinstance(mw, PluginSkillsMiddleware)]
        assert len(skills_mws) >= 1
        subagent_sources = [s if isinstance(s, str) else s[0] for s in skills_mws[0].sources]
        assert any("earnings-reviewer" in sp for sp in subagent_sources)

        # Coordinator agent middleware has PluginSkillsMiddleware
        coord_mws = call_kwargs.get("middleware", [])
        coord_skills_mws = [mw for mw in coord_mws if isinstance(mw, PluginSkillsMiddleware)]
        assert len(coord_skills_mws) >= 1
        live_skills, _ = coord_skills_mws[0]._get_live_skills()
        live_skill_names = {s.get("name") for s in live_skills}
        # Coordinator has vertical and partner skills, but NOT agent plugin skill
        assert "earnings-analysis" not in live_skill_names
