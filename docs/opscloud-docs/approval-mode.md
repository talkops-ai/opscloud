# Approval Modes and Security

> Human-in-the-loop governance: Manual, Auto, and Smart modes with multi-layer scanners

By default, OpsCloud requires explicit human confirmation before executing any tool that could alter your workspace or cloud environment. These gated actions include:

- Modifying or deleting workspace files
- Running mutating or destructive shell commands
- Making outbound network requests
- Executing privileged or mutating MCP tools

Read-only inspection operations (`ls`, `read_file`, `glob`, `grep_search`, `aws sts get-caller-identity`, `kubectl get`) execute without human prompts. Approval modes let you choose the governance model appropriate for your session.

---

## Approval Modes Overview

| Mode | What it does | Flag / Invocation | Primary Use Case |
|---|---|---|---|
| **Manual** (default) | Prompts for confirmation before every gated action | Default in TUI | Production clusters, live cloud accounts, and multi-tenant platforms |
| **Auto** | Uses primary LLM classifier to evaluate action safety | `-y`, `--auto-approve`, or `--approval-mode auto` | Non-interactive headless scripts (`-p`), local development tasks |
| **Smart** | Uses **TypeSafe AI Jev System One** classifier (<100ms) | `--smart` or `--approval-mode smart` | Interactive developer sessions with sub-second safety gating |

Cycle between modes at any time during an interactive session using **`Shift+Tab`**:

```text
Manual ──> Auto ──> Smart ──> Manual
```

---

## Manual Mode

Manual mode is the safe default for interactive sessions. Whenever a mutating tool call is initiated, execution pauses and displays an interactive modal in the terminal:

```text
[Approve]  [Reject]  [Edit Command]  [Always Allow]
```

- **Approve**: Executes the tool call with proposed parameters.
- **Reject**: Aborts the tool call and returns structured rejection context to the agent so it can revise its plan.
- **Edit Command**: Allows the operator to modify shell arguments, file paths, or parameters directly before execution.
- **Always Allow**: Adds the tool or command pattern to the current session's runtime allowlist.

Manual mode is recommended for all operations involving production infrastructure, cloud IAM policies, and shared Kubernetes clusters.

---

## Auto Mode

Auto mode delegates safety evaluation to the primary LLM model. Before executing a gated tool, the model evaluates whether the action complies with the prompt's intent and safety constraints.

Launch in Auto mode:

```bash
opscloud -y
# or:
opscloud --auto-approve
# or:
opscloud --approval-mode auto
```

> [!NOTE]
> Headless and non-interactive executions (`opscloud -p "..."`) default to Auto mode to enable autonomous execution while retaining model-level safety checks.

---

## Smart Mode (TypeSafe AI Jev System One)

Smart mode provides sub-second semantic safety gating (<100ms) powered by **TypeSafe AI Jev System One**. 

Instead of waiting for a multi-second LLM inference roundtrip to decide whether to prompt the operator, Jev evaluates the proposed action against calibrated safety boundaries:

- **`mutating_probability`**: Analyzes the semantic likelihood that a command alters system state.
- **`blast_radius`**: Assesses resource scope (local scratch file vs. shared VPC or cluster-wide deletion).
- **`risk_level`**: Evaluates risk tier (0 = Safe, 1 = Controlled/Reversible, 2 = Critical).
- **`requires_human_interrupt`**: Emits a deterministic interrupt verdict.

Safe inspections (`kubectl get`, `terraform plan`, `aws s3 ls`, `read_file`) execute immediately. High-risk or destructive mutations pause for operator approval.

Launch in Smart mode:

```bash
opscloud --smart
# or:
opscloud --approval-mode smart
```

Or configure it as default in `~/.opscloud/config.toml`:

```toml
[startup]
mode = "smart"
```

---

## Per-Thread Mode Persistence

Your active approval mode is stored in the thread checkpoint state. When resuming an earlier session with `opscloud -r <thread_id>`, OpsCloud restores the exact approval mode that was active when the thread was last used.

---

## Shell Allowlists

You can pre-approve specific binaries so they execute without interactive prompts regardless of approval mode:

```bash
# Allow specific binaries
opscloud -S "terraform,tofu,kubectl,helm"

# Use the curated recommended allowlist
opscloud -S recommended

# Allow all shell commands
opscloud -S all
```

Or configure allowlists in `~/.opscloud/config.toml`:

```toml
[tools]
shell_allow_list = ["tofu", "terraform", "kubectl", "helm"]
```

---

## Security Layers

OpsCloud applies multi-layered automated validation before any command executes:

### 1. Shell Safety Classification & AST Scanner
Every shell command undergoes AST analysis to detect dangerous constructs (`rm -rf /`, privilege escalation, unauthorized background forks).

### 2. Unicode Security Scanner
Tool arguments and file edits are scanned for invisible zero-width characters, homoglyph look-alikes, and bidirectional overrides (Trojan Source attacks).

### 3. URL and SSRF Guard
Outbound network requests are verified against an SSRF guard blocking link-local metadata endpoints (`169.254.169.254`, `metadata.google.internal`), loopback addresses, and private RFC-1918 subnets.

### 4. Headless MCP Security Guard
MCP tools are grouped into four security tiers (`READ_ONLY`, `MUTATING_SAFE`, `MUTATING_DESTRUCTIVE`, `PRIVILEGED`) with strict policy enforcement during headless operations.

### 5. Subagent Tool Scoping
Dynamic subagents are restricted to their declared toolsets, preventing privilege escalation across domain boundaries.
