"""Unit tests for subagent metadata and prompt block generation."""

from opscloud.middleware.subagents import SubagentsMiddleware


def test_subagents_middleware():
    metas = [
        {"name": "eks-specialist", "description": "Diagnoses EKS cluster and node issues"},
        {"name": "cost-optimizer", "description": "Analyzes AWS spend and right-sizing"},
    ]
    mw = SubagentsMiddleware(subagent_metas=metas)
    assert mw.subagent_names == ["cost-optimizer", "eks-specialist"]

    prompt_block = mw._build_prompt_block()
    assert "## Subagent Delegation & Orchestration Architecture" in prompt_block
    assert "### 1. Built-in Subagents (Direct `task` Tool)" in prompt_block
    assert "`eks-specialist`" in prompt_block
    assert "`cost-optimizer`" in prompt_block

    # Test dynamic registration of plugin subagent
    mw.register_subagent({
        "name": "plugin-secops",
        "description": "Security compliance scanner",
        "source": "plugin:cloud-security",
        "skills": ["cve-scan"],
    })
    assert "plugin-secops" in mw.subagent_names
    prompt_block_with_plugin = mw._build_prompt_block()
    assert "### 2. Plugin & Extension Subagents (Code Interpreter `js_eval`)" in prompt_block_with_plugin
    assert "`plugin-secops`" in prompt_block_with_plugin
    assert "`cve-scan`" in prompt_block_with_plugin

    # Test before_agent state injection
    state_update = mw.before_agent({}, None)
    assert state_update is not None
    assert "_subagent_registry" in state_update
    assert "eks-specialist" in state_update["_subagent_registry"]
    assert state_update["_subagent_registry"]["plugin-secops"]["is_plugin"] is True

    # Test planning_mode
    mw_planning = SubagentsMiddleware(subagent_metas=metas, planning_mode=True)
    prompt_block_planning = mw_planning._build_prompt_block()
    assert "## Subagent Delegation & Operational Capabilities" in prompt_block_planning
    assert "`eks-specialist`" in prompt_block_planning


def test_get_built_in_subagents_empty():
    """Ensure OpsCloud has no built-in subagents on disk."""
    from opscloud.subagents import get_built_in_subagents

    assert get_built_in_subagents() == []


def test_parse_subagent_file(tmp_path):
    """Test parsing subagent markdown with YAML frontmatter."""
    from opscloud.subagents.loader import parse_subagent_file

    # Valid subagent file
    agent_file = tmp_path / "AGENTS.md"
    agent_file.write_text(
        """---
name: eks-operator
description: Kubernetes and EKS cluster management specialist
model: anthropic:claude-3-5-sonnet-20241022
skills: k8s-diag, helm-ops
tools: kubectl, helm, read_file
permission_tier: tier2
capabilities:
  - cluster_admin
  - log_stream
mcp_config:
  k8s-mcp:
    command: k8s-mcp-server
---

# EKS Operator

You are an expert Kubernetes cluster administrator.
Analyze pod status and diagnose node health.
""",
        encoding="utf-8",
    )

    parsed = parse_subagent_file(agent_file)
    assert parsed is not None
    assert parsed["name"] == "eks-operator"
    assert parsed["description"] == "Kubernetes and EKS cluster management specialist"
    assert parsed["model"] == "anthropic:claude-3-5-sonnet-20241022"
    assert parsed["skills"] == ["k8s-diag", "helm-ops"]
    assert parsed["tools"] == ["kubectl", "helm", "read_file"]
    assert parsed["permission_tier"] == "tier2"
    assert parsed["capabilities"] == ["cluster_admin", "log_stream"]
    assert parsed["mcp_config"] == {"k8s-mcp": {"command": "k8s-mcp-server"}}
    assert "You are an expert Kubernetes cluster administrator." in parsed["system_prompt"]
    assert parsed["source"] == "custom"
    assert parsed["path"] == str(agent_file)


def test_parse_subagent_file_fallback_and_invalid(tmp_path):
    """Test edge cases: missing frontmatter, fallback name, missing description."""
    from opscloud.subagents.loader import parse_subagent_file

    # Missing frontmatter
    no_fm = tmp_path / "plain.md"
    no_fm.write_text("# Just Markdown", encoding="utf-8")
    assert parse_subagent_file(no_fm) is None

    # Missing description
    no_desc = tmp_path / "no_desc.md"
    no_desc.write_text("---\nname: my-agent\n---\nPrompt", encoding="utf-8")
    assert parse_subagent_file(no_desc) is None

    # Fallback name engagement
    fallback_file = tmp_path / "fallback.md"
    fallback_file.write_text("---\ndescription: Fallback test\n---\nPrompt", encoding="utf-8")
    parsed = parse_subagent_file(fallback_file, fallback_name="inferred-agent")
    assert parsed is not None
    assert parsed["name"] == "inferred-agent"
    assert parsed["description"] == "Fallback test"


def test_list_subagents_precedence(tmp_path, monkeypatch):
    """Test user vs project directory precedence and plugin discovery."""
    from opscloud.subagents.loader import list_subagents

    user_dir = tmp_path / "user_agents"
    user_dir.mkdir()
    proj_dir = tmp_path / "proj_agents"
    proj_dir.mkdir()

    # User agent: eks-specialist (will be overridden) and cost-optimizer (will remain)
    (user_dir / "eks-specialist").mkdir()
    (user_dir / "eks-specialist" / "AGENTS.md").write_text(
        "---\ndescription: User EKS agent\n---\nUser prompt", encoding="utf-8"
    )
    (user_dir / "cost-optimizer").mkdir()
    (user_dir / "cost-optimizer" / "AGENTS.md").write_text(
        "---\ndescription: User cost optimizer\n---\nUser cost prompt", encoding="utf-8"
    )

    # Project agent overrides eks-specialist
    (proj_dir / "eks-specialist").mkdir()
    (proj_dir / "eks-specialist" / "AGENTS.md").write_text(
        "---\ndescription: Project EKS agent override\n---\nProject prompt", encoding="utf-8"
    )

    # Mock plugin discovery
    mock_plugin_agent = {
        "name": "terraform-plugin:tf-planner",
        "description": "Terraform plan validator",
        "system_prompt": "Run tf plan",
        "source": "plugin",
        "path": "/fake/path/AGENTS.md",
    }
    monkeypatch.setattr(
        "opscloud.plugins.adapters.agents.discover_plugin_subagents",
        lambda project_root=None: [mock_plugin_agent],
    )

    results = list_subagents(
        user_agents_dir=user_dir,
        project_agents_dir=proj_dir,
        include_plugins=True,
    )

    result_by_name = {r["name"]: r for r in results}
    assert "eks-specialist" in result_by_name
    # Project should override user
    assert result_by_name["eks-specialist"]["description"] == "Project EKS agent override"
    assert result_by_name["eks-specialist"]["source"] == "project"

    assert "cost-optimizer" in result_by_name
    assert result_by_name["cost-optimizer"]["description"] == "User cost optimizer"
    assert result_by_name["cost-optimizer"]["source"] == "user"

    assert "terraform-plugin:tf-planner" in result_by_name
    assert result_by_name["terraform-plugin:tf-planner"]["source"] == "plugin"


def test_load_async_subagents(tmp_path, monkeypatch):
    """Test loading remote async subagents from config.toml with env expansion."""
    from opscloud.subagents.loader import load_async_subagents

    monkeypatch.setenv("OPSCLOUD_REMOTE_HOST", "cloud.internal")
    monkeypatch.setenv("OPSCLOUD_AUTH_TOKEN", "bearer-xyz123")

    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[async_subagents.remote_secops]
description = "Remote security compliance scanner"
graph_id = "secops-scanner"
url = "https://${OPSCLOUD_REMOTE_HOST}/api/v1"

[async_subagents.remote_secops.headers]
Authorization = "Bearer ${OPSCLOUD_AUTH_TOKEN}"
""",
        encoding="utf-8",
    )

    agents = load_async_subagents(config_file)
    assert len(agents) == 1
    secops = agents[0]
    assert secops["name"] == "remote_secops"
    assert secops["description"] == "Remote security compliance scanner"
    assert secops["graph_id"] == "secops-scanner"
    assert secops["url"] == "https://cloud.internal/api/v1"
    assert secops["headers"] == {"Authorization": "Bearer bearer-xyz123"}

