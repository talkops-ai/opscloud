# Subagents

> Dynamic domain subagents with isolated memory, scoped tools, and real-time telemetry

OpsCloud delegates specialized, heavy, or multi-domain tasks to dynamic subagents. Rather than bundling rigid, hardcoded subagents, OpsCloud utilizes a fully extensible **dynamic subagent architecture**. Subagents are loaded on demand from agent plugins, project directories, user home configurations, and remote services.

Each subagent operates with its own system prompt, isolated memory sandbox (`SubagentMemoryStore`), scoped tool permissions, and domain skills. Intermediate searches, compiler outputs, and diagnostic iterations remain confined to the subagent's execution branch—only the final, validated deliverable returns to the orchestrator.

---

## Dynamic Subagent Architecture

In OpsCloud, built-in subagents are not statically compiled or bundled. Domain operators are discovered dynamically at runtime:

```
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

### Discovery and Precedence

Subagents are resolved in order of priority:

1. **Agent Plugins** (Marketplace and local manifests): Plugins providing an `agents/` directory bundle domain definitions, scoped skills, and isolated MCP servers (namespaced e.g. `plugin-id:agent-name`).
2. **Project Subagents**: `.opscloud/agents/` or `.agents/` inside the active worktree (committed to Git, takes precedence over user subagents).
3. **User Subagents**: `~/.opscloud/agents/` or `~/.agents/` on the local machine (workstation-wide).
4. **Async Remote Subagents**: Background agents declared in `config.toml` under `[async_subagents]`.

---

## Subagent Definition Format

Subagents are authored as Markdown files with YAML frontmatter. OpsCloud accepts:
- `.opscloud/agents/{name}/AGENTS.md` (or `{name}.md`)
- `.opscloud/agents/{name}.md`

### Example Definition

```markdown
---
name: eks-cluster-operator
description: Specialist in Kubernetes cluster health diagnostics, node group capacity, and add-on upgrades
model: anthropic:claude-3-5-sonnet-20241022
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
| `mcp_files` | No | List of paths to `.mcp.json` files bundled with the subagent |
| `capabilities` | No | Explicit capability tags used for dynamic task matching |

---

## Memory Isolation (`SubagentMemoryStore`)

Each subagent runs within an isolated `SubagentMemoryStore`. Verbose tool logs, API payloads, and temporary reasoning remain isolated to the subagent's execution branch. This guarantees:
- No context contamination in the primary conversation thread
- Preservation of token budgets for long-running workflows
- Structured deliverables passed back to the root orchestrator without losing tables or commands

---

## Subagent Context Compaction

OpsCloud runs `CLICompactionMiddleware` across both the root orchestrator and individual subagents.

When a subagent performs intensive operations (such as auditing hundreds of cloud resources or iterating on complex manifests), context compaction triggers automatically:
1. Intermediate conversation turns are summarized.
2. Complete historical context is offloaded to disk.
3. Execution continues with an active, token-efficient window.

---

## System Tool Whitelist

Subagents are restricted to their declared tools via `ToolFilterMiddleware`. However, OpsCloud guarantees that `ALWAYS_ALLOWED_SYSTEM_TOOLS` (such as `compact_conversation` and `ask_user`) are whitelisted across all subagents so memory compaction and operator clarification are never blocked.

---

## Live Telemetry and Cost Rollup

The Textual TUI includes a dedicated `SubagentPanel` providing real-time visibility into multi-agent operations:
- Active subagent identifier and execution status
- Running tool indicators
- Turn duration and millisecond latency
- Live token usage and cumulative USD cost rollup across all active subagents

---

## Launching and Switching Subagents

### Direct CLI Launch
Start an OpsCloud session focused on a specific subagent:

```bash
opscloud -a eks-cluster-operator
```

### Hot-Swapping in Interactive TUI
Inside an active session, use `/agents` to open the interactive agent selector and switch subagents dynamically.
