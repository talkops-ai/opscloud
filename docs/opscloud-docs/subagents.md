# Subagents

> Dynamic domain subagents with isolated memory, scoped tools, and real-time telemetry

OpsCloud delegates specialized, heavy, or multi-domain tasks to dynamic subagents. Rather than bundling rigid, hardcoded subagents in the binary, OpsCloud utilizes a fully extensible **dynamic subagent architecture**. Subagents are loaded on demand from agent plugins, project directories, user home configurations, and remote services.

Each subagent operates with its own domain prompt, isolated memory sandbox (`SubagentMemoryStore`), scoped tool permissions, and domain skills. Intermediate searches, raw cloud telemetry, and diagnostic iterations remain confined to the subagent's execution branch—only the final, structured deliverable returns to the orchestrator.

---

## Dynamic Subagent Architecture

```text
                    ┌───────────────────────────────┐
                    │    Root Orchestration Agent    │
                    │   (Context, Router & State)   │
                    └───────────────┬───────────────┘
                                    │
        ┌──────────────┬────────────┼────────────┬──────────────┐
        ▼              ▼            ▼            ▼              ▼
  ┌───────────┐  ┌───────────┐┌───────────┐┌───────────┐  ┌───────────┐
  │  Project  │  │  Plugin   ││   User    ││   Async   │  │ Dynamic   │
  │ Subagent  │  │ Subagent  ││ Subagent  ││  Remote   │  │ Fan-Out   │
  └─────┬─────┘  └─────┬─────┘└─────┬─────┘└─────┬─────┘  └─────┬─────┘
        │              │            │            │              │
        └──────────────┴────────────┼────────────┴──────────────┘
                                    ▼
                      ┌───────────────────────────┐
                      │    SubagentMemoryStore    │
                      │ (Isolated Memory & State) │
                      └───────────────────────────┘
```

### Discovery & Precedence

Subagents are resolved in order of priority:

1. **Agent Plugins** (Marketplace and local manifests): Plugins providing an `agents/` directory bundle domain definitions, scoped skills, and isolated MCP servers (e.g. `aws-sre-agent`, `aws-finops-agent`).
2. **Project Subagents**: `.opscloud/agents/` or `.agents/` inside the active worktree (committed to Git, takes precedence over user subagents).
3. **User Subagents**: `~/.opscloud/agents/` or `~/.agents/` on the local machine (workstation-wide).
4. **Async Remote Subagents**: Background agents declared in `config.toml` under `[async_subagents]`.

---

## Subagent Definition Format

Subagents are authored as Markdown files with YAML frontmatter:
- `.opscloud/agents/{name}.md`
- `.opscloud/agents/{name}/AGENTS.md`

### Example Definition

```markdown
---
name: eks-cluster-operator
description: Specialist in Kubernetes cluster health diagnostics, node group capacity, and add-on upgrades
model: anthropic:claude-3-5-sonnet-latest
skills:
  - aws-eks-autopilot
  - kubernetes-diagnostics
tools:
  - execute
  - read_file
  - edit_file
permission_tier: controlled
---

You are an expert EKS operations subagent. Analyze cluster health, node group capacity, and add-on versions before recommending or applying changes.

Always inspect live cluster state with `kubectl get` and `aws eks describe-cluster` before proposing any node group or manifest changes. Produce structured audit tables and actionable commands.
```

### Frontmatter Schema

| Field | Required | Description |
|---|---|---|
| `name` | Yes | Unique identifier for the subagent |
| `description` | Yes | High-level summary of capabilities used by the orchestrator for autonomous delegation |
| `model` | No | Dedicated model override in `provider:model-name` format |
| `skills` | No | List of skill names to pre-load for this subagent |
| `tools` | No | Tool allowlist patterns (e.g. `read_file, execute, glob, grep_search`) |
| `permission_tier` | No | Permission level (`read-only`, `controlled`, `full`) |
| `mcp_config` | No | Embedded dictionary of MCP server configurations for isolated toolsets |
| `capabilities` | No | Explicit capability tags used for dynamic task matching |

---

## Memory Isolation (`SubagentMemoryStore`)

Each subagent runs within an isolated `SubagentMemoryStore`. Verbose tool logs, massive API payloads, and temporary reasoning remain isolated to the subagent's execution branch:
- **No context contamination**: The main orchestrator thread remains clean and focused.
- **Budget preservation**: Subagent turns do not inflate the root conversation token count.
- **Structured deliverables**: Subagent audit findings, data tables, and actionable CLI commands are returned intact to the operator.

---

## Dual-Level Context Compaction

When subagent tasks involve long investigations or hundreds of command iterations, `CLICompactionMiddleware` triggers compaction independently:
- **Root Compaction**: Automatically summarizes and offloads old conversation turns in the main thread.
- **Subagent Compaction**: Summarizes deep investigative tool turns inside the subagent so it can continue operating without context exhaustion.

---

## Launching Subagents

### Direct Launch via CLI

Launch OpsCloud directly into a subagent:

```bash
opscloud -a aws-finops-agent "Audit unattached EBS volumes in us-west-2"
```

### Interactive Switching

Inside an active TUI session:

```text
/agents           # Open the interactive subagent picker
/agents reset     # Reset the active subagent's conversation branch
```

The TUI includes a dedicated **Subagent Panel** dock displaying running subagents, active tools, turn timing, and cumulative token costs.
