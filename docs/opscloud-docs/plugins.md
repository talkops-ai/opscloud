# Plugins and marketplaces

> Extend OpsCloud with plugins that bundle skills, subagents, MCP servers, slash commands, and hooks

Plugins extend OpsCloud with reusable skills, specialized subagents, MCP server configurations, custom slash commands, and lifecycle hooks. Depending on their contents and scope, OpsCloud categorizes plugins into three distinct types:

| Type | What it adds | How it binds |
|---|---|---|
| **Agent plugins** | A dedicated subagent with domain skills, MCP servers, and tool permissions | Skills and MCP tools bind exclusively to the dedicated subagent |
| **Vertical plugins** | Skills, custom slash commands, and lifecycle hooks — no subagent | Skills and commands bind directly to the main root orchestrator |
| **Partner-built plugins**| External vendor integrations bundling skills and MCP connectors | Skills and MCP tools bind to the root agent or designated subagent |

OpsCloud detects plugin types automatically: if the plugin package contains an `agents/` directory, it is treated as an **agent plugin**; otherwise, its capabilities augment the root orchestrator directly.

> [!WARNING]
> Install plugins and marketplace sources only from verified, trusted sources. An enabled plugin can execute shell commands, launch MCP processes, and register custom instructions with your workstation user permissions.

---

## Manage plugins interactively

Manage plugins directly inside any interactive OpsCloud session:

1. Type `/plugins` to open the plugin manager modal.
2. Navigate to the **Marketplaces** tab to add a source. Supported marketplace formats:
   * A GitHub repository: `owner/repo`, optionally with `@branch-or-tag`
   * An HTTPS Git URL: `https://github.com/...#branch`
   * An HTTPS endpoint serving a `marketplace.json` manifest
   * A local filesystem path or local JSON file
3. Browse available plugins and click **Install**.
4. Type `/reload` in the chat input to activate newly installed skills and MCP servers without restarting your session.

The plugin manager also allows you to enable, disable, and uninstall plugins. Disabling a plugin keeps its files cached locally but unregisters its skills, agents, and tools.

---

## Manage plugins from the command line

Use `opscloud plugin` for scripting and terminal administration. Plugin identifiers follow the `plugin-name@marketplace-name` format:

```bash
# Add a marketplace source
opscloud plugin marketplace add https://marketplace.talkops.ai/plugins.json
opscloud plugin marketplace list

# List installed and available plugins
opscloud plugin list

# Install a plugin
opscloud plugin install terraform-linter@talkops-cloud-toolkit

# Toggle plugin state
opscloud plugin disable terraform-linter@talkops-cloud-toolkit
opscloud plugin enable terraform-linter@talkops-cloud-toolkit

# Uninstall a plugin
opscloud plugin uninstall terraform-linter@talkops-cloud-toolkit
```

---

## Plugin types in detail

### 1. Agent plugins

An agent plugin bundles a **dedicated subagent** with its own isolated memory, tool permissions, and domain skills. The subagent runs in complete isolation — its MCP servers and skills do not pollute the root orchestrator's context window.

**Example directory structure:**
```text
terraform-linter/
├── plugin.json
├── agents/
│   └── terraform-linter.md    # Subagent definition (AGENTS.md format)
└── skills/
    ├── tf-fmt-check/
    │   └── SKILL.md
    └── tf-validate/
        └── SKILL.md
```

**`agents/terraform-linter.md`:**
```markdown
---
name: terraform-linter
description: Lints and validates Terraform modules for formatting, syntax errors, and corporate standards
tools: read_file, execute, glob, grep
skills:
  - tf-fmt-check
  - tf-validate
permission_tier: read-write
---

You are the **Terraform Linter** subagent.

## Workflow
1. Discover `.tf` files with `glob`.
2. Verify formatting using the `tf-fmt-check` skill.
3. Validate syntax using the `tf-validate` skill.
4. Output a concise markdown report of findings.
```

### 2. Vertical plugins

A vertical plugin augments the root orchestrator directly without introducing a separate subagent. It provides specialized skills and custom slash commands:

```text
aws-finops/
├── plugin.json
└── skills/
    └── cost-optimization/
        └── SKILL.md
```

---

## The `plugin.json` manifest

Every plugin requires a `plugin.json` manifest at its root:

```json
{
  "name": "terraform-linter",
  "version": "1.2.0",
  "description": "Linting and syntax validation suite for Terraform and OpenTofu",
  "author": "TalkOps Platform Team",
  "skills": ["skills/tf-fmt-check", "skills/tf-validate"],
  "agents": ["agents/terraform-linter.md"],
  "mcp": ".mcp.json"
}
```

---

## Authoring and publishing plugins

1. Create a local plugin directory matching the structure above.
2. Test your plugin locally by linking it into your workspace:
   ```bash
   mkdir -p .opscloud/plugins
   ln -s /path/to/my-plugin .opscloud/plugins/my-plugin
   ```
3. Verify tool and skill registration with `opscloud plugin list` and `opscloud skills list`.
4. Publish the plugin by committing it to a Git repository and adding it to your team's custom marketplace catalog.
