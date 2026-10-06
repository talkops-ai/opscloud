# Overview

```text
  ___  ____  ____   ____ _     ___  _   _ ____  
 / _ \|  _ \/ ___| / ___| |   / _ \| | | |  _ \ 
| | | | |_) \___ \| |   | |  | | | | | | | | | |
| |_| |  __/ ___) | |___| |__| |_| | |_| | |_| |
 \___/|_|   |____/ \____|_____\___/ \___/|____/ 
```

> **Extensible Terminal Multi-Agent Framework for Cloud Operations & DevOps Coding**

OpsCloud is an extensible, terminal-based multi-agent framework that unifies platform engineering coding and multi-cloud operations with strict human-in-the-loop governance:

1. **Native Platform & DevOps Coding (Main Agent)**: Built directly into the root Deep Agent. Reads codebases, writes features, fixes bugs, generates Infrastructure as Code (Terraform, OpenTofu, CDK), authors Kubernetes manifests, and builds CI/CD pipelines. Adheres strictly to: **produce reviewable diffs and plans, never blind unreviewed deployments**.
2. **Cloud Operations via Specialist Subagents (Plugin Marketplace)**: Extensible multi-agent framework connected directly to the **TalkOps DevOps Plugin Marketplace** (`talkops-ai/devops-plugins`). Spawns specialized domain subagents (SRE, FinOps, Cloud Security, Database) or domain skill packs across AWS, Azure, GCP, and Kubernetes to inspect live infrastructure, diagnose incidents, and optimize cloud systems.

OpsCloud works with 22+ LLM providers, features the ultra-low latency **TypeSafe AI Jev Dynamic Model Router (<70ms)** and **Jev System One Safety Classifier (<100ms)**, and provides dynamic subagent delegation with isolated memory sandboxes (`SubagentMemoryStore`).

---

## Quick Install

```bash
# Install via script
curl -LsSf https://raw.githubusercontent.com/talkops-ai/opscloud/main/scripts/install.sh | bash

# Or install from PyPI
pip install talkops-opscloud

# Launch the interactive terminal UI
opscloud
```

See the [Quickstart](./quickstart.md) for credential setup and interactive usage.

---

## The Two Core Pillars

### Pillar 1: Built-in Coding Agent (Deep Agent + Platform Skills)

- **Infrastructure as Code (IaC)**: Authors and refactors production-grade Terraform (HCL), OpenTofu, Terragrunt, AWS CDK (TypeScript/Python), CloudFormation, and Pulumi. Enforces modular design, remote state locking, provider version pinning, and dry-run validation.
- **Cloud-Native & Container Orchestration**: Generates and patches Kubernetes manifests (Deployments, StatefulSets, Ingress, NetworkPolicies, CRDs), Helm charts, Kustomize overlays, Dockerfiles, and compose configurations with non-root security contexts and resource boundaries.
- **CI/CD & GitOps Automation**: Authors and debugs GitHub Actions workflows, GitLab CI/CD pipelines, ArgoCD Application/ApplicationSet manifests, and Tekton pipelines with pinned actions and secret masking.
- **Platform Tooling & Automation**: Writes robust bash automation, Python platform tools (boto3, click, typer), Makefiles, and operational CLI utilities.
- **Policy as Code & Observability**: Authors OPA/Rego policies, Kyverno rules, Prometheus alert specifications, Datadog/CloudWatch monitor definitions, and Grafana dashboard JSON models.

### Pillar 2: Cloud Operations via Plugin Marketplace

OpsCloud connects directly to the **TalkOps DevOps Plugin Marketplace** (`talkops-ai/devops-plugins`):
- **Agent Plugins**: Self-contained specialist subagents (`aws-finops-agent`, `aws-sre-agent`, `aws-iac-engineer`, `aws-cloud-security-engineer`, `aws-database-engineer`, `aws-platform-engineer`). The orchestrator delegates complex cloud missions to them automatically.
- **Vertical Plugins**: Domain skill packs that attach directly to the main agent for specialized areas like `aws-networking`, `aws-containers`, `aws-cost-optimization`, and `aws-observability`.
- **Partner Plugins**: Official third-party skills, such as HashiCorp's official Terraform skill pack.

---

## Core System Architecture

### 1. Dynamic System Prompts
Composed dynamically using template-based generation:
- **Model Identity Injection**: Injects model name, provider, token context window limits, and modality constraints.
- **Cloud Provider Awareness**: Dynamically tailors guidance for AWS, Azure, GCP, or Multi-Cloud environments.
- **Operational Discipline**: Enforces surgical edits (`edit_file` over sed/awk), idempotency, and strict artifact vs. chat separation.

### 2. Multi-Tier Approval Modes
- **Manual Mode** *(default)*: Prompts for confirmation on every mutating or risky action.
- **Auto Mode**: Evaluates tool calls using primary LLM reasoning. Default for non-interactive (`-p`) runs.
- **Smart Mode**: Evaluates semantic blast radius and mutation likelihood in <100ms via **TypeSafe AI Jev System One**. Cycle modes at runtime using `Shift+Tab`.

### 3. Layered Middleware Pipeline
Every turn executes through a strict 17-stage LangGraph middleware stack:
1. `ConfigurableModelMiddleware` & `JevDynamicModelRouterMiddleware` (<70ms dynamic model tiering)
2. `CodeModelRetryMiddleware` (recovers from transient API failures)
3. `GlmTerminalStallRecoveryMiddleware` & `HeadlessMCPGuardMiddleware` (4-tier MCP security)
4. `ResumeStateMiddleware`, `CostTrackingMiddleware`, `GoalToolsMiddleware`
5. `AskUserMiddleware` (interactive human-in-the-loop clarifications)
6. `MCPContextMiddleware` & `MCPToolMiddleware` (dynamic MCP server lifecycle)
7. `MemoryMiddleware` & `ManagedMemoryGuardMiddleware` (`AGENTS.md` and guarded paths)
8. `PluginSkillsMiddleware` (on-demand skill resolution from local, project, and plugin sources)
9. `CodeInterpreterMiddleware` (optional QuickJS sandbox for `js_eval`)
10. `LocalContextMiddleware` (working directory, Git branch, and LangSmith tracing)
11. `ShellAllowListMiddleware` (AST command scanning against allowlists)
12. `AutoModeHITLMiddleware` / `AsyncApprovalHITLMiddleware` (approval gating)
13. `ServerHooksMiddleware` (pre/post-tool lifecycle execution via `hooks.json`)
14. `GoalCriteriaMiddleware` (interactive objective criteria tracking)
15. `CLICompactionMiddleware` (summarizes and offloads deep histories)
16. `ReliableRubricMiddleware` (closed-loop rubric grading and verification)
17. `UnifiedSystemMessageMiddleware` (clean system message consolidation)

### 4. Jev System One Fast Classification & Routing
- **Smart Approval Gate (<100ms)**: Calculates `mutating_probability`, `blast_radius`, and `risk_level` without LLM generation latency.
- **Dynamic Model Router (<70ms)**: Classifies incoming prompt complexity and dynamically routes to the appropriate model tier (`fast`, `standard`, `powerful`).
- **Hybrid Rubric Grader**: Combines a <200ms Jev System One fast-pass with a Frontier LLM diagnostic fallback for failed acceptance criteria.

### 5. Dynamic Subagents & Isolation
- **No hardcoded subagents**: Discovered from agent plugins, project `.opscloud/agents/`, user `~/.opscloud/agents/`, or async remote configs.
- **SubagentMemoryStore**: Heavy exploratory logs, large JSON dumps, and lint iterations stay isolated to the subagent's execution branch; only clean deliverables return to the orchestrator.
- **Dual-Level Compaction**: `CLICompactionMiddleware` runs independently on both the root orchestrator and individual subagents.

### 6. Remote Cloud Sandboxes
Run workloads in isolated ephemeral cloud containers via `--sandbox`:
- Supported providers: **Modal**, **Daytona**, **LangSmith**, **AgentCore**, **Runloop**, **Vercel**, and local **Docker**.
- Intelligent routing: local execution for web search / documentation fetch; remote container execution for shell commands and filesystem mutations.
- Bi-directional workspace synchronization with automatic exclusion of build artifacts and caches.

---

## Core Built-in Tools

| Tool | Description |
|---|---|
| `execute` | Run shell commands with stdout/stderr capture, timeouts, and multi-layer AST safety checks |
| `read_file` / `write_file` / `edit_file` | Read, create, and edit files with precision chunk replacements |
| `delete` | Remove files safely (gated behind explicit human approval) |
| `glob` / `grep_search` / `ls` | Search file paths and regex contents across the workspace |
| `web_search` | Search official documentation, CVE advisories, error codes, and cloud API specifications (via Tavily) |
| `fetch_url` | Extract markdown content from technical documentation URLs with SSRF protection |
| `js_eval` | Evaluate JavaScript in an in-memory QuickJS interpreter for dynamic scripting and fanout |
| `get_goal` / `update_goal` | Inspect and update interactive goal acceptance criteria |
| `get_rubric` | Retrieve rubric specifications for automated self-evaluation loops |

---

## DevOps Environment Awareness

OpsCloud automatically isolates and preserves your cloud environment:
- **Kubernetes**: `KUBECONFIG`, `KUBE_CONTEXT`
- **AWS**: `AWS_PROFILE`, `AWS_REGION`, `AWS_DEFAULT_REGION`, `AWS_SHARED_CREDENTIALS_FILE`
- **GCP**: `GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_CLOUD_PROJECT`, `CLOUDSDK_CORE_PROJECT`
- **Azure**: `AZURE_SUBSCRIPTION_ID`, `AZURE_TENANT_ID`
- **Ansible**: `ANSIBLE_CONFIG`, `ANSIBLE_INVENTORY`
- **Helm**: `HELM_HOME`, `HELM_REPOSITORY_CONFIG`
- **ArgoCD**: `ARGOCD_SERVER`, `ARGOCD_AUTH_TOKEN`
- **Terraform / OpenTofu**: `TF_CLI_CONFIG_FILE`, `TERRAGRUNT_CONFIG`

---

## Configuration & Data Locations

OpsCloud uses pure filesystem-backed persistence with zero external database dependencies:

| Path | Contents |
|---|---|
| `~/.opscloud/config.toml` | Global user configuration (model defaults, Jev router pools, UI, permissions) |
| `~/.opscloud/.env` | Global user credentials saved with `0600` permissions via `/auth` |
| `~/.opscloud/hooks.json` | Global lifecycle event hooks |
| `~/.opscloud/.mcp.json` | Global MCP server definitions |
| `~/.opscloud/plugins/` | Installed marketplace plugins |
| `~/.opscloud/agents/` | Global custom subagent definitions (`.md` with YAML frontmatter) |
| `~/.opscloud/skills/` | Global custom skills (`SKILL.md`) |
| `~/.opscloud/.state/sessions.db` | SQLite conversation checkpoint database |
| `~/.opscloud/.state/history.jsonl` | Interactive command history |
| `.opscloud/` | Project-scoped overrides (`config.toml`, `.env`, `agents/`, `skills/`, `hooks.json`, `.mcp.json`) |
