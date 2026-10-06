# Model Context Protocol (MCP) Tools

> Connect OpsCloud to external developer tools, cloud SDKs, and enterprise systems using MCP

OpsCloud implements the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/). MCP provides an open standard to connect external tool providers — Kubernetes cluster inspectors, Terraform registries, AWS SDKs, database servers, and internal developer platforms — directly into the agent without authoring custom Python middleware.

---

## Add an MCP server

MCP servers are configured in JSON files. Create an `.mcp.json` in your project root or in `~/.opscloud/`:

```json
{
  "mcpServers": {
    "kubernetes": {
      "command": "npx",
      "args": ["-y", "@kubernetes/mcp-server"],
      "env": {
        "KUBECONFIG": "/Users/user/.kube/config"
      }
    },
    "aws": {
      "command": "npx",
      "args": ["-y", "@aws/mcp-server"],
      "env": {
        "AWS_PROFILE": "production",
        "AWS_REGION": "us-west-2"
      }
    }
  }
}
```

Each server entry defines:

| Field | Type | Description |
|---|---|---|
| `command` | string | Executable to launch (`npx`, `python`, `uvx`, `docker`) |
| `args` | list | Command-line arguments passed to the server |
| `env` | object | Environment variables injected into the server process |

---

## Where to place MCP configuration

OpsCloud checks for MCP configurations in the following order (first found wins):

1. **CLI flag** (highest priority): `opscloud --mcp-config path/to/mcp.json`
2. **Project-level**: `.mcp.json`, `mcp.json`, `.opscloud/.mcp.json`, or `.opscloud/mcp.json`
3. **User-level**: `~/.opscloud/.mcp.json`

---

## Tool naming convention

When OpsCloud starts, it launches configured MCP servers, discovers their tools, and registers them using namespaced identifiers:

```text
mcp__{server}__{tool}
```

For example, a tool named `get_pods` from server `kubernetes` becomes `mcp__kubernetes__get_pods`.

Use the `/mcp` slash command inside a session to inspect active servers, loaded tools, parameter schemas, and connection health.

---

## Interactive TUI MCP viewer

Inside the interactive TUI, type:

```text
/mcp
```

The MCP viewer modal provides:
- Live list of connected MCP servers and status indicators
- Searchable list of registered tools
- Full JSON parameter schemas and descriptions
- Ability to test tools manually before agent execution

---

## Trust and security

### Global vs. project trust

- **Global servers** (`~/.opscloud/.mcp.json`) are always trusted since they reside in your user home directory.
- **Project servers** (`.opscloud/.mcp.json`, `.mcp.json`) require confirmation on first use because they can be checked into Git repositories by external contributors. OpsCloud records your trust decisions in `~/.opscloud/.state/mcp_trust.json`.

Override trust for automated CI/CD:

```bash
# Trust project MCP servers for this session
opscloud --trust-project-mcp

# Disable all MCP tool loading
opscloud --no-mcp
```

### Headless MCP security guard

When running unattended (in headless mode `-n` or CI/CD pipelines), OpsCloud automatically classifies each MCP tool into security tiers:

| Tier | Behavior in Headless Mode | Examples |
|---|---|---|
| **`READ_ONLY`** | Executes automatically | `mcp__k8s__get_pods`, `mcp__aws__describe_instances` |
| **`MUTATING_SAFE`** | Allowed in Auto and Smart modes | `mcp__k8s__apply_manifest`, `mcp__aws__tag_resource` |
| **`MUTATING_DESTRUCTIVE`**| Blocked unless explicitly allowlisted | `mcp__k8s__delete_namespace`, `mcp__aws__terminate_instances` |
| **`PRIVILEGED`** | Blocked in unattended mode | Operations modifying cluster RBAC or root IAM policies |

---

## Subagent MCP servers

Dynamic subagents and agent plugins can bundle their own embedded MCP server configurations (via embedded `.mcp.json` or YAML frontmatter `mcp_config`).

These servers are managed automatically by OpsCloud's session lifecycle:
- Launch on-demand when the subagent is invoked
- Scope tools exclusively to the active subagent
- Terminate cleanly when the subagent completes its task
