# Overview

> The AI Coding & Cloud Operations Agent for Your Terminal

OpsCloud brings two essential engineering capabilities together into a single terminal agent:

1. **A Hands-On Coding Agent**: Powered by the main Deep Agent and its built-in platform skills. OpsCloud reads your codebase, writes new features, fixes bugs, generates Infrastructure as Code (Terraform, OpenTofu, CDK), authors Kubernetes manifests, and builds CI/CD pipelines. It adheres to a safe, responsible principle: **produce reviewable diffs and plans, never blind unreviewed deployments**.
2. **Autonomous Cloud Operations**: Instead of locking you into a monolithic set of tools, OpsCloud connects directly to the **TalkOps DevOps Plugin Marketplace** (`talkops-ai/devops-plugins`). With a single command, you can install specialized AI agents (like SRE, FinOps, Cloud Security, and Database engineers) or domain skill packs across AWS, Azure, GCP, and Kubernetes to inspect live environments, diagnose incidents, and optimize cloud infrastructure.

OpsCloud works with 22+ LLM providers (Anthropic, AWS Bedrock, OpenAI, Google Gemini, and more), features the low-latency **Jev TypeSafe Dynamic Model Router (<70ms)** and **Jev System One Safety Classifier (<100ms)**, and provides dynamic subagent delegation with isolated memory stores and dual-level context compaction.

---

## Quick Install

```bash
curl -LsSf https://opscloud.talkops.ai/install.sh | bash

# Launch the interactive terminal UI
opscloud
```

See the [Quickstart](./quickstart.md) for credential setup and interactive usage.

---

## The Two Core Pillars

### Pillar 1: Built-in Coding Agent (Deep Agent + Platform Skills)

- **Infrastructure as Code (IaC)**: Authors and refactors production-grade Terraform (HCL), OpenTofu, Terragrunt, AWS CDK (TypeScript and Python), CloudFormation, and Pulumi. Adheres to modular design, remote state locking, provider version pinning, and dry-run validation.
- **Cloud-Native & Container Orchestration**: Generates and patches Kubernetes manifests (Deployments, StatefulSets, Ingress, NetworkPolicies, CRDs), Helm charts, Kustomize overlays, Dockerfiles, and container compose files with non-root security contexts and resource boundaries.
- **CI/CD & GitOps Automation**: Authors and debugs GitHub Actions workflows, GitLab CI/CD pipelines, ArgoCD Application/ApplicationSet manifests, and Tekton pipelines with pinned actions and secret masking.
- **Platform Tooling & Automation**: Writes robust bash automation, Python platform tools (boto3, click, typer), Makefiles, and operational CLI utilities.
- **Policy as Code & Observability**: Authors OPA/Rego policies, Kyverno rules, Prometheus alert specifications, Datadog/CloudWatch monitor definitions, and Grafana dashboard JSON models.

### Pillar 2: Cloud Operations via the Plugin Marketplace

OpsCloud connects directly to the **TalkOps DevOps Plugin Marketplace** (`talkops-ai/devops-plugins`):
- **Agent Plugins**: Self-contained autonomous specialist subagents (e.g. `aws-finops-agent`, `aws-sre-agent`, `aws-iac-engineer`, `aws-cloud-security-engineer`, `aws-database-engineer`, `aws-platform-engineer`). The orchestrator delegates complex cloud missions to them automatically.
- **Vertical Plugins**: Domain skill packs that attach directly to the main agent for specialized areas like `aws-networking`, `aws-containers`, `aws-cost-optimization`, and `aws-observability`.
- **Partner Plugins**: Official third-party skills, such as HashiCorp's official Terraform skill pack.

---

## Core Tools

OpsCloud includes built-in tools for filesystem operations, shell execution, web search, and objective tracking:

| Tool | Description |
|---|---|
| `execute` | Run shell commands with stdout/stderr capture, execution timeouts, and multi-layer safety checks |
| `read_file` / `write_file` / `edit_file` | Read, create, and edit files with precision chunk replacements |
| `delete` | Remove files safely (gated behind explicit human approval) |
| `glob` / `grep_search` / `ls` | Search file paths and regex contents across the workspace |
| `web_search` | Search official documentation, CVE advisories, error codes, and cloud API specifications |
| `fetch_url` | Extract markdown content from technical documentation URLs with SSRF protection |
| `js_eval` | Evaluate JavaScript in an in-memory QuickJS interpreter for dynamic scripting and fanout |
| `get_goal` / `update_goal` | Inspect and update interactive goal acceptance criteria |
| `get_rubric` | Retrieve rubric specifications for autonomous self-evaluation loops |

---

## Dynamic Subagent Architecture

OpsCloud does not bundle rigid, hardcoded subagents. Instead, domain operators are discovered dynamically at runtime from:

1. **Agent Plugins**: Bundled with an `agents/` directory or explicit agent manifests, providing domain system prompts, scoped skills, and isolated MCP servers.
2. **Project Definitions**: Worktree-level agents placed in `.opscloud/agents/` or `.agents/`.
3. **User Definitions**: Machine-level agents configured in `~/.opscloud/agents/` or `~/.agents/`.
4. **Async Remote Subagents**: Declared in `config.toml` under `[async_subagents]`.

Each subagent runs with memory isolation (`SubagentMemoryStore`). Intermediate exploratory searches, compiler logs, and linting iterations remain confined to the subagent's execution branch—only the final, validated deliverable returns to the orchestrator. See [Subagents](./subagents.md).

---

## Platform Features

| Feature | Description |
|---|---|
| **Dual-Engine Architecture** | Combines platform engineering/coding with autonomous cloud operations |
| **Jev Dynamic Router (<70ms)** | Classifies request complexity and routes to Fast, Standard, or Powerful model tiers in under 70ms |
| **Jev System One Safety Gate (<100ms)** | Calibrated semantic blast-radius and mutation likelihood classifier for sub-second safety decisions |
| **Dual Context Compaction** | `CLICompactionMiddleware` automatically summarizes deep histories on both root and subagent turns |
| **Live Telemetry & Cost Rollup** | `SubagentPanel` displays live streaming status, tool progress, and token cost accumulation |
| **System Tool Whitelisting** | `ALWAYS_ALLOWED_SYSTEM_TOOLS` guarantees subagents can always compact context and ask user questions |
| **Unified Reasoning Effort** | Standardized `/effort` levels (`off`, `low`, `medium`, `high`) mapped across Anthropic, OpenAI, Gemini, and DeepSeek |
| **Persistent Memory** | Workspace-scoped memory and SQLite checkpoints carry context across sessions |
| **Skill Resolution Hierarchy** | Progressive disclosure allows skills to load on-demand without bloating token budgets |
| **MCP Integration** | Connect external tools via Model Context Protocol with automated 4-tier security classification |
| **Plugins & Marketplaces** | Install community or private enterprise plugins bundling skills, subagents, and MCP servers |
| **3 Approval Modes** | Manual, Auto, and Smart modes with live switching via `Shift+Tab` |
| **Autonomous Rubric Grading** | Pair worker agents with dedicated grader models to enforce delivery specs in CI/CD |
| **Remote Cloud Sandboxes** | Execute untrusted code or heavy builds in ephemeral containers (Modal, Daytona, AgentCore) |
| **Lifecycle Hooks** | Run deterministic pre-tool and post-tool scripts via `hooks.json` |

---

## DevOps Environment Awareness

OpsCloud automatically detects and preserves your infrastructure environment:

- **Kubernetes**: `KUBECONFIG`, `KUBE_CONTEXT`
- **AWS**: `AWS_PROFILE`, `AWS_REGION`, `AWS_DEFAULT_REGION`, `AWS_SHARED_CREDENTIALS_FILE`
- **GCP**: `GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_CLOUD_PROJECT`, `CLOUDSDK_CORE_PROJECT`
- **Azure**: `AZURE_SUBSCRIPTION_ID`, `AZURE_TENANT_ID`
- **Ansible**: `ANSIBLE_CONFIG`, `ANSIBLE_INVENTORY`
- **Helm**: `HELM_HOME`, `HELM_REPOSITORY_CONFIG`
- **ArgoCD**: `ARGOCD_SERVER`, `ARGOCD_AUTH_TOKEN`
- **Terraform / OpenTofu**: `TF_CLI_CONFIG_FILE`, `TERRAGRUNT_CONFIG`

It also recognizes project markers (`terragrunt.hcl`, `Chart.yaml`, `ansible.cfg`, `.opscloud/`) alongside standard repository files (`.git`, `pyproject.toml`, `package.json`, `Makefile`).

---

## Architecture & Turn Pipeline

OpsCloud turn execution pipeline:
1. **Safety & Security**: Shell AST parsing, Unicode Trojan Source scanning, SSRF protection
2. **Context & Discovery**: Git status, DevOps environment variables, and progressive skill discovery
3. **Jev Model Router**: Low-latency classification (<70ms) to assign the appropriate model tier
4. **Execution & Checkpointing**: SQLite thread persistence, dynamic subagent delegation, and background context compaction
5. **Telemetry**: Real-time token usage, duration timing, and USD cost calculations

### Data Locations

| Path | Contents |
|---|---|
| `~/.opscloud/config.toml` | Global user configuration |
| `~/.opscloud/agents/` | Global custom subagent definitions |
| `~/.opscloud/skills/` | Global custom skills |
| `~/.opscloud/plugins/` | Installed agent plugins |
| `.opscloud/` | Project-scoped configuration, subagents, skills, and hooks |
| `~/.opscloud/state.db` | Checkpoint database storing threads and approval modes |
