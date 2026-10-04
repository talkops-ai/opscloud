"""Comprehensive tests for project-local plugin auto-discovery and bifurcation.

Covers:
- Project plugin loading and marketplace discovery
- Plugin bifurcation (agent vs non-agent)
- Subagent metadata construction and skill/MCP isolation
- Non-agent plugin component mapping (skills, commands, MCP servers)
- End-to-end verification against the reference/financial-services marketplace (19 plugins)
- Resilient JSON repair on malformed MCP configs in real plugins
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opscloud.plugins.project_plugins import (
    ProjectPluginResult,
    _collect_skill_names,
    _has_agents,
    load_project_plugins,
)


def _make_sample_agent_plugin(
    root: Path,
    name: str,
    *,
    skill_names: list[str] | None = None,
    mcp_servers: dict | None = None,
) -> Path:
    """Create a minimal agent plugin directory tree."""
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True, exist_ok=True)

    manifest_dir = plugin_dir / ".claude-plugin"
    manifest_dir.mkdir(exist_ok=True)
    (manifest_dir / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.0.0"}), encoding="utf-8"
    )

    agents_dir = plugin_dir / "agents"
    agents_dir.mkdir(exist_ok=True)
    agent_body = f"# {name}\nYou are an expert agent specialized in {name}."
    (agents_dir / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: Test agent {name}\ntools:\n  - read_file\n---\n{agent_body}",
        encoding="utf-8",
    )

    if skill_names:
        skills_dir = plugin_dir / "skills"
        for sname in skill_names:
            s_path = skills_dir / sname
            s_path.mkdir(parents=True, exist_ok=True)
            (s_path / "SKILL.md").write_text(
                f"---\nname: {sname}\ndescription: Skill {sname}\n---\nSkill content",
                encoding="utf-8",
            )

    if mcp_servers is not None:
        (plugin_dir / ".mcp.json").write_text(
            json.dumps({"mcpServers": mcp_servers}), encoding="utf-8"
        )

    return plugin_dir


def _make_sample_non_agent_plugin(
    root: Path,
    name: str,
    *,
    skill_names: list[str] | None = None,
    command_names: list[str] | None = None,
    mcp_servers: dict | None = None,
) -> Path:
    """Create a non-agent vertical plugin (skills, commands, MCP servers)."""
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True, exist_ok=True)

    manifest_dir = plugin_dir / ".claude-plugin"
    manifest_dir.mkdir(exist_ok=True)
    (manifest_dir / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.0.0"}), encoding="utf-8"
    )

    if skill_names:
        skills_dir = plugin_dir / "skills"
        for sname in skill_names:
            s_path = skills_dir / sname
            s_path.mkdir(parents=True, exist_ok=True)
            (s_path / "SKILL.md").write_text(
                f"---\nname: {sname}\ndescription: Skill {sname}\n---\nContent",
                encoding="utf-8",
            )

    if command_names:
        commands_dir = plugin_dir / "commands"
        commands_dir.mkdir(exist_ok=True)
        for cname in command_names:
            (commands_dir / f"{cname}.md").write_text(
                f"# {cname}\nCommand description.", encoding="utf-8"
            )

    if mcp_servers is not None:
        (plugin_dir / ".mcp.json").write_text(
            json.dumps({"mcpServers": mcp_servers}), encoding="utf-8"
        )

    return plugin_dir


def test_empty_directory_returns_empty_result(tmp_path: Path):
    result = load_project_plugins(tmp_path)
    assert isinstance(result, ProjectPluginResult)
    assert len(result.subagent_metas) == 0
    assert len(result.main_skill_sources) == 0
    assert len(result.main_commands) == 0
    assert len(result.main_mcp_configs) == 0


def test_bifurcation_agent_vs_non_agent(tmp_path: Path):
    """Test that agent plugins become subagents with isolated skills, while non-agent plugins feed main agent."""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()

    # 1. Agent plugin
    _make_sample_agent_plugin(
        plugins_dir,
        "pitch-agent",
        skill_names=["slide-builder", "presentation-design"],
        mcp_servers={"pitchbook": {"command": "npx"}},
    )

    # 2. Non-agent plugin
    _make_sample_non_agent_plugin(
        plugins_dir,
        "financial-analysis",
        skill_names=["dcf-model", "lbo-analysis"],
        command_names=["dcf", "lbo"],
        mcp_servers={"morningstar": {"command": "npx"}},
    )

    # Create marketplace manifest
    mp_dir = tmp_path / ".claude-plugin"
    mp_dir.mkdir()
    (mp_dir / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "finance-pack",
                "plugins": [
                    {"name": "pitch-agent", "source": "./plugins/pitch-agent"},
                    {"name": "financial-analysis", "source": "./plugins/financial-analysis"},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = load_project_plugins(tmp_path)

    # 1. Subagent created for pitch-agent
    assert len(result.subagent_metas) == 1
    subagent = result.subagent_metas[0]
    assert subagent["name"] == "pitch-agent@finance-pack"
    assert "expert agent specialized in pitch-agent" in subagent["system_prompt"]

    # 2. Skill isolation:
    # pitch-agent skills should NOT be in main_skill_sources!
    main_skill_paths = [s[0] for s in result.main_skill_sources]
    assert not any("slide-builder" in p for p in main_skill_paths)

    # financial-analysis skills SHOULD be in main_skill_sources
    assert any("financial-analysis" in p for p in main_skill_paths)

    # 3. Command mapping:
    # financial-analysis commands should be in main_commands
    cmd_files = [f.stem for cmd_path in result.main_commands for f in cmd_path.glob("*.md")]
    assert "dcf" in cmd_files
    assert "lbo" in cmd_files

    from opscloud.plugins.adapters.commands import discover_plugin_commands
    handlers = discover_plugin_commands(tmp_path)
    handler_names = [h.name for h in handlers]
    assert "/dcf" in handler_names
    assert "/lbo" in handler_names

    # 4. MCP mapping:
    # Morningstar should be in main_mcp_configs
    mcp_servers_combined: dict[str, dict] = {}
    for cfg in result.main_mcp_configs:
        mcp_servers_combined.update(cfg.get("mcpServers", {}))
    assert "morningstar" in mcp_servers_combined


def test_load_real_financial_services_reference_marketplace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify full end-to-end discovery of reference/financial-services without modifying reference files."""
    monkeypatch.setattr(
        "opscloud.plugins.store._user_settings_path", lambda: tmp_path / "settings.json"
    )
    repo_root = Path(__file__).resolve().parent.parent.parent
    fin_root = repo_root / "reference" / "financial-services"

    if not fin_root.exists():
        pytest.skip("reference/financial-services not found in environment")

    result = load_project_plugins(fin_root)

    # 1. Total subagents: exactly 10 agent plugins from plugins/agent-plugins/
    assert len(result.subagent_metas) == 10
    subagent_names = [s["name"] for s in result.subagent_metas]

    expected_agents = [
        "pitch-agent",
        "market-researcher",
        "earnings-reviewer",
        "meeting-prep-agent",
        "model-builder",
        "gl-reconciler",
        "kyc-screener",
        "valuation-reviewer",
        "month-end-closer",
        "statement-auditor",
    ]
    for agent in expected_agents:
        assert any(agent in name for name in subagent_names), f"Missing expected agent {agent}"

    # Verify each subagent has a valid non-empty system prompt loaded from its agents/*.md
    for sub in result.subagent_metas:
        assert sub.get("system_prompt"), f"Subagent {sub['name']} has empty system_prompt"

    # 2. Main agent skills from vertical plugins (financial-analysis, investment-banking, etc.)
    assert len(result.main_skill_sources) >= 5
    skill_labels = [s[1] for s in result.main_skill_sources]
    assert any("financial-analysis" in label for label in skill_labels)
    assert any("investment-banking" in label for label in skill_labels)

    # 3. Main agent slash commands (/dcf, /comps, /lbo, etc.)
    cmd_files = [f.stem for cmd_path in result.main_commands for f in cmd_path.glob("*.md")]
    assert "dcf" in cmd_files
    assert "comps" in cmd_files
    assert "lbo" in cmd_files

    from opscloud.plugins.adapters.commands import discover_plugin_commands
    handlers = discover_plugin_commands(fin_root)
    handler_names = [h.name for h in handlers]
    assert "/dcf" in handler_names
    assert "/comps" in handler_names
    assert "/lbo" in handler_names

    # 4. Resilient MCP parsing: financial-analysis/.mcp.json has missing comma and unclosed brace
    # Verify that its MCP servers were successfully repaired and loaded!
    mcp_servers_combined: dict[str, dict] = {}
    for cfg in result.main_mcp_configs:
        mcp_servers_combined.update(cfg.get("mcpServers", {}))

    assert "daloopa" in mcp_servers_combined
    assert "box" in mcp_servers_combined
    assert "morningstar" in mcp_servers_combined
    assert "factset" in mcp_servers_combined
    assert "sp-global" in mcp_servers_combined
