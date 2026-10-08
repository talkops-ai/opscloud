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

from langchain_core.tools import tool
from opscloud.agent.factory import create_opscloud_agent
from opscloud.mcp.discovery import MCPDiscovery
from opscloud.middleware.mcp_context import MCPContextMiddleware
from opscloud.middleware.mcp_middleware import MCPToolMiddleware
from opscloud.middleware.skills import PluginSkillsMiddleware
from opscloud.middleware.tool_filter import ToolFilterMiddleware
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

        # Dynamic subagent has tools list defined and MCP middlewares attached
        assert "tools" in matching_sub, "Subagent must have 'tools' key in its spec"
        assert isinstance(matching_sub["tools"], list)
        sub_mcp_ctx = [mw for mw in mw_list if isinstance(mw, MCPContextMiddleware)]
        assert len(sub_mcp_ctx) == 1, "Subagent must have MCPContextMiddleware attached"
        sub_mcp_tool = [mw for mw in mw_list if isinstance(mw, MCPToolMiddleware)]
        assert len(sub_mcp_tool) == 1, "Subagent must have MCPToolMiddleware attached"

        # Coordinator agent middleware has PluginSkillsMiddleware
        coord_mws = call_kwargs.get("middleware", [])
        coord_skills_mws = [mw for mw in coord_mws if isinstance(mw, PluginSkillsMiddleware)]
        assert len(coord_skills_mws) >= 1
        live_skills, _ = coord_skills_mws[0]._get_live_skills()
        live_skill_names = {s.get("name") for s in live_skills}
        # Coordinator has vertical and partner skills, but NOT agent plugin skill
        assert "earnings-analysis" not in live_skill_names

        # Coordinator has MCPContextMiddleware and MCPToolMiddleware from partner plugin
        coord_mcp_ctx = [mw for mw in coord_mws if isinstance(mw, MCPContextMiddleware)]
        assert len(coord_mcp_ctx) == 1, "Coordinator must have MCPContextMiddleware when partner MCP plugin is present"
        coord_mcp_tool = [mw for mw in coord_mws if isinstance(mw, MCPToolMiddleware)]
        assert len(coord_mcp_tool) == 1, "Coordinator must have MCPToolMiddleware when partner MCP plugin is present"


@pytest.mark.asyncio
async def test_subagent_mcp_tools_and_middleware_scoping_with_preloaded_tools(tmp_path: Path) -> None:
    """Verify subagent receives preloaded MCP tools and proper MCP middleware without leaking to coordinator."""
    plugins_dir = tmp_path / "plugins"

    # Create agent plugin with multiple MCP servers matching finops pattern
    agent_dir = plugins_dir / "finops"
    (agent_dir / "agents").mkdir(parents=True, exist_ok=True)
    (agent_dir / "plugin.json").write_text(
        json.dumps({"name": "finops", "version": "1.0.0"}),
        encoding="utf-8",
    )
    (agent_dir / "agents" / "finops.md").write_text(
        "---\n"
        "name: finops\n"
        "description: FinOps cost specialist\n"
        "tools: Read, Bash, mcp__plugin_finops_billing__*, mcp__plugin_finops_aws-mcp__*, mcp__plugin_finops_awspricing__*\n"
        "---\n"
        "FinOps agent prompt.\n",
        encoding="utf-8",
    )
    (agent_dir / ".mcp.json").write_text(
        json.dumps({
            "mcpServers": {
                "aws-mcp": {"type": "stdio", "command": "uvx", "args": ["aws-mcp"]},
                "billing": {"type": "stdio", "command": "uvx", "args": ["billing"]},
                "awspricing": {"type": "stdio", "command": "uvx", "args": ["awspricing"]},
            }
        }),
        encoding="utf-8",
    )

    @tool("billing_get_cost_and_usage")
    def mock_billing_tool(query: str) -> str:
        """Query billing cost and usage."""
        return "cost data"

    @tool("aws-mcp_describe_regions")
    def mock_aws_mcp_tool() -> str:
        """Describe AWS regions."""
        return "us-east-1"

    @tool("awspricing_get_products")
    def mock_awspricing_tool(service: str) -> str:
        """Get product pricing."""
        return "pricing data"

    @tool("coordinator_custom_tool")
    def mock_coordinator_tool() -> str:
        """Coordinator exclusive tool."""
        return "coord"

    mock_mcp_tools = [mock_billing_tool, mock_aws_mcp_tool, mock_awspricing_tool]

    from langchain_core.language_models.chat_models import BaseChatModel

    mock_model = MagicMock(spec=BaseChatModel)
    mock_model.bind_tools = MagicMock(return_value=mock_model)
    mock_model.profile = {}

    with (
        patch("opscloud.plugins.adapters.mcp.prepare_subagent_mcp") as mock_prep_mcp,
        patch("opscloud.agent.factory.create_deep_agent") as mock_create,
    ):
        mock_info1 = MagicMock()
        mock_info1.name = "aws-mcp"
        mock_info2 = MagicMock()
        mock_info2.name = "billing"
        mock_info3 = MagicMock()
        mock_info3.name = "awspricing"
        mock_prep_mcp.return_value = (
            {"aws-mcp": {}, "billing": {}, "awspricing": {}},
            mock_mcp_tools,
            [mock_info1, mock_info2, mock_info3],
        )
        mock_create.return_value = MagicMock()

        agent_graph, backend = create_opscloud_agent(
            model=mock_model,
            cwd=tmp_path,
            auto_approve=True,
            interactive=False,
            tools=[mock_coordinator_tool],
        )

        assert mock_create.called
        call_kwargs = mock_create.call_args.kwargs
        coord_tools = call_kwargs.get("tools", [])
        subagents = call_kwargs.get("subagents", [])

        # 1. Verify coordinator tools contain coordinator tools but NOT subagent MCP tools
        coord_tool_names = {getattr(t, "name", str(t)) for t in coord_tools}
        assert "coordinator_custom_tool" in coord_tool_names
        assert "billing_get_cost_and_usage" not in coord_tool_names
        assert "aws-mcp_describe_regions" not in coord_tool_names
        assert "awspricing_get_products" not in coord_tool_names

        # 2. Verify subagent has its own tools list populated with actual tool objects
        finops_subs = [
            s for s in subagents
            if isinstance(s, dict) and (s.get("name") == "finops" or s.get("name", "").startswith("finops@"))
        ]
        assert len(finops_subs) == 1
        finops_sub = finops_subs[0]

        assert "tools" in finops_sub, "Subagent must have 'tools' key in its spec"
        sub_tools = finops_sub["tools"]
        sub_tool_names = {getattr(t, "name", str(t)) for t in sub_tools}

        # Assert actual tool objects are bound (not just string placeholders)
        assert mock_billing_tool in sub_tools
        assert mock_aws_mcp_tool in sub_tools
        assert mock_awspricing_tool in sub_tools

        assert "billing_get_cost_and_usage" in sub_tool_names
        assert "aws-mcp_describe_regions" in sub_tool_names
        assert "awspricing_get_products" in sub_tool_names

        # 3. Verify subagent middleware stack has MCPContextMiddleware and MCPToolMiddleware
        sub_mws = finops_sub.get("middleware", [])
        assert any(isinstance(mw, MCPContextMiddleware) for mw in sub_mws), (
            "Subagent middleware must include MCPContextMiddleware"
        )
        assert any(isinstance(mw, MCPToolMiddleware) for mw in sub_mws), (
            "Subagent middleware must include MCPToolMiddleware"
        )

        # 4. Verify ToolFilterMiddleware allows subagent's MCP tools
        tool_filter_mws = [mw for mw in sub_mws if isinstance(mw, ToolFilterMiddleware)]
        assert len(tool_filter_mws) == 1
        tf = tool_filter_mws[0]
        assert tf.is_tool_allowed("billing_get_cost_and_usage", mock_billing_tool)
        assert tf.is_tool_allowed("aws-mcp_describe_regions", mock_aws_mcp_tool)
        assert tf.is_tool_allowed("awspricing_get_products", mock_awspricing_tool)


def test_coordinator_deep_agent_mcp_binding_and_isolation(tmp_path: Path):
    """Verify coordinator deep agent binds MCP tools & middleware without leaking subagent MCP tools."""
    from langchain_core.tools import tool
    from opscloud.agent.factory import create_opscloud_agent
    from opscloud.middleware.mcp_context import MCPContextMiddleware
    from opscloud.middleware.mcp_middleware import MCPToolMiddleware
    from opscloud.middleware.server_hooks import ServerHooksMiddleware

    # Coordinator's MCP tool (e.g. from a system/partner plugin or .mcp.json)
    @tool("github_create_issue")
    def mock_github_tool(title: str) -> str:
        """Create a github issue."""
        return "issue_123"

    mock_github_tool.metadata = {
        "_mcp_server": "github",
        "_mcp_original_name": "create_issue",
    }

    # Subagent's MCP tool (e.g. from an agent plugin)
    @tool("billing_get_cost_and_usage")
    def mock_billing_tool(query: str) -> str:
        """Query billing cost and usage."""
        return "cost data"

    mock_billing_tool.metadata = {
        "_mcp_server": "billing",
        "_mcp_original_name": "get_cost_and_usage",
    }

    # Set up mock subagent directory
    agents_dir = tmp_path / "agents" / "finops"
    agents_dir.mkdir(parents=True)
    (agents_dir / "finops.md").write_text(
        "---\n"
        "name: finops\n"
        "description: FinOps specialist\n"
        "tools: Read, Bash, billing:*\n"
        "---\n"
        "You are FinOps subagent.\n",
        encoding="utf-8",
    )

    from langchain_core.language_models.chat_models import BaseChatModel

    mock_model = MagicMock(spec=BaseChatModel)
    mock_model.bind_tools = MagicMock(return_value=mock_model)
    mock_model.profile = {}

    with (
        patch("opscloud.plugins.adapters.mcp.prepare_subagent_mcp") as mock_prep_mcp,
        patch("opscloud.agent.factory.create_deep_agent") as mock_create,
    ):
        mock_prep_mcp.return_value = (
            {"billing": {"command": "uvx", "args": ["billing-server"]}},
            [mock_billing_tool],
            [MagicMock(name="billing")],
        )
        mock_create.return_value = MagicMock()

        # Invoke factory with coordinator MCP tools and config
        create_opscloud_agent(
            model=mock_model,
            cwd=tmp_path,
            auto_approve=True,
            interactive=False,
            mcp_tools=[mock_github_tool],
            mcp_config={"github": {"command": "npx", "args": ["github-mcp"]}},
        )

        assert mock_create.called
        call_kwargs = mock_create.call_args.kwargs
        coord_tools = call_kwargs.get("tools", [])
        coord_mws = call_kwargs.get("middleware", [])
        subagents = call_kwargs.get("subagents", [])

        # 1. Coordinator tools verify:
        # - Has coordinator MCP tool
        # - Does NOT leak subagent MCP tool
        assert mock_github_tool in coord_tools
        assert mock_billing_tool not in coord_tools

        # 2. Coordinator middleware verify:
        # - Has MCPContextMiddleware
        # - Has MCPToolMiddleware
        # - Has ServerHooksMiddleware with mcp_tools
        assert any(isinstance(mw, MCPContextMiddleware) for mw in coord_mws)
        assert any(isinstance(mw, MCPToolMiddleware) for mw in coord_mws)
        hooks_mws = [mw for mw in coord_mws if isinstance(mw, ServerHooksMiddleware)]
        assert len(hooks_mws) == 1
        assert "github_create_issue" in hooks_mws[0]._mcp_servers
        assert hooks_mws[0]._mcp_servers["github_create_issue"] == "github"

        # 3. Subagent tools and middleware verify:
        finops_sub = next(s for s in subagents if s.get("name") == "finops")
        sub_tools = finops_sub["tools"]
        sub_mws = finops_sub["middleware"]

        # - Has subagent MCP tool
        # - Does NOT have coordinator MCP tool
        assert mock_billing_tool in sub_tools
        assert mock_github_tool not in sub_tools

        # - Has MCPContextMiddleware and MCPToolMiddleware
        assert any(isinstance(mw, MCPContextMiddleware) for mw in sub_mws)
        assert any(isinstance(mw, MCPToolMiddleware) for mw in sub_mws)

        # - ToolFilterMiddleware allows subagent MCP tool
        tf = next(mw for mw in sub_mws if isinstance(mw, ToolFilterMiddleware))
        assert tf.is_tool_allowed("billing_get_cost_and_usage", mock_billing_tool) is True
        assert tf.is_tool_allowed("billing:get_cost_and_usage") is True
        # Coordinator tool blocked for subagent
        assert tf.is_tool_allowed("github_create_issue", mock_github_tool) is False

