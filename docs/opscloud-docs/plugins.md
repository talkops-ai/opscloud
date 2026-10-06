# Plugins and Marketplaces

> Extend OpsCloud with plugins bundling skills, subagents, MCP servers, and hooks

Plugins extend OpsCloud with reusable skills, specialized subagents, MCP server configurations, custom slash commands, and lifecycle hooks. Instead of bloating the core binary, OpsCloud connects directly to the **TalkOps DevOps Plugin Marketplace** ([`talkops-ai/devops-plugins`](https://github.com/talkops-ai/devops-plugins)) and supports private enterprise marketplaces.

---

## Plugin Types

OpsCloud categorizes plugins into three distinct types based on package contents:

| Type | What it Adds | How it Binds | Examples |
|---|---|---|---|
| **Agent Plugins** | Dedicated specialist subagent with domain skills & MCP servers | Binds exclusively to the dedicated subagent with memory isolation | `aws-sre-agent`, `aws-finops-agent`, `aws-iac-engineer` |
| **Vertical Plugins** | Domain skill packs and lifecycle hooks (no separate subagent) | Binds directly to the main root orchestrator | `aws-networking`, `aws-containers`, `aws-cost-optimization` |
| **Partner Plugins** | Official third-party vendor integrations bundling tools & skills | Binds to the root agent or designated subagent | HashiCorp official Terraform skill collection |

OpsCloud detects plugin types automatically: if the plugin package contains an `agents/` directory, it is treated as an **agent plugin**; otherwise, its capabilities augment the root orchestrator directly.

---

## TalkOps DevOps Plugin Marketplace

The official marketplace repository is [`talkops-ai/devops-plugins`](https://github.com/talkops-ai/devops-plugins). It provides curated specialist subagents:

- **`aws-finops-agent`**: Audits cloud spend, identifies idle EBS volumes/NAT gateways, analyzes Savings Plans, and writes cost-optimization diffs.
- **`aws-sre-agent`**: Investigates CloudWatch alarms, traces distributed errors with X-Ray, analyzes logs, and pinpoints root causes.
- **`aws-iac-engineer`**: Architects, validates, and deploys CDK, CloudFormation, and Terraform infrastructure.
- **`aws-cloud-security-engineer`**: Audits IAM policies, inspects Security Hub/GuardDuty findings, and remediates vulnerabilities.
- **`aws-database-engineer`**: Manages migrations, tunes queries, and provisions Aurora, DynamoDB, and RDS instances.
- **`aws-platform-engineer`**: Manages EKS clusters, ECS services, VPC networking, and edge routing.

---

## Manage Plugins via CLI

Use `opscloud plugin` for scripting and terminal administration:

```bash
# Add the official TalkOps marketplace
opscloud plugin marketplace add talkops-ai/devops-plugins

# List available plugins in configured marketplaces
opscloud plugin marketplace list

# Install specialist subagents
opscloud plugin install aws-sre-agent
opscloud plugin install aws-finops-agent

# Install vertical domain skill packs
opscloud plugin install aws-networking

# List installed plugins and status
opscloud plugin list

# Enable or disable an installed plugin
opscloud plugin disable aws-finops-agent
opscloud plugin enable aws-finops-agent

# Uninstall a plugin
opscloud plugin uninstall aws-finops-agent
```

---

## Manage Plugins Interactively

Manage plugins directly inside any interactive OpsCloud session:

1. Type `/plugins` to open the plugin manager modal.
2. Navigate to the **Marketplaces** tab to browse or add sources. Supported marketplace formats:
   - A GitHub repository: `owner/repo` (e.g. `talkops-ai/devops-plugins`), optionally with `@branch-or-tag`
   - An HTTPS Git URL: `https://github.com/...#branch`
   - An HTTPS endpoint serving a `marketplace.json` manifest
   - A local filesystem path to a marketplace directory
3. Select any plugin to view its description, bundled skills, and MCP tools, then click **Install**.
4. Type `/reload` in the chat input to activate newly installed components without restarting your session.

---

## Plugin Package Structure

### Agent Plugin Structure

An agent plugin bundles a **dedicated subagent** with isolated memory (`SubagentMemoryStore`), scoped MCP tools, and domain skills:

```text
aws-finops-agent/
├── plugin.json                # Plugin metadata, dependencies, author
├── agents/
│   └── aws-finops-agent.md    # Subagent definition (YAML frontmatter + prompt)
├── skills/
│   ├── aws-cost-optimization/
│   │   └── SKILL.md
│   └── aws-billing-audit/
│       └── SKILL.md
└── .mcp.json                  # Isolated MCP servers for this subagent
```

### Vertical Plugin Structure

A vertical plugin bundles skills and hooks directly attached to the orchestrator:

```text
aws-networking/
├── plugin.json
└── skills/
    ├── vpc-design/
    │   └── SKILL.md
    └── transit-gateway-routing/
        └── SKILL.md
```

---

## Plugin Manifest (`plugin.json`)

Each plugin root contains a `plugin.json` manifest:

```json
{
  "name": "aws-finops-agent",
  "version": "1.0.0",
  "description": "Autonomous cloud financial management and cost optimization agent",
  "author": "TalkOps AI",
  "homepage": "https://github.com/talkops-ai/devops-plugins",
  "dependencies": [],
  "subagents": ["aws-finops-agent"],
  "skills": ["aws-cost-optimization", "aws-billing-audit"],
  "mcp_servers": {
    "aws-cost": {
      "command": "uvx",
      "args": ["aws-cost-mcp-server"]
    }
  }
}
```
