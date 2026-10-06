<div align="center">

<img src="docs/assets/opscloud_logo.png" alt="OpsCloud" width="120">

# OpsCloud

**The AI Coding & Cloud Operations Agent for Your Terminal**

*Full-stack coding via Deep Agents + autonomous cloud operations across AWS, Azure, and GCP through the TalkOps DevOps Plugin Marketplace.*

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-3776AB.svg?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![LangGraph](https://img.shields.io/badge/orchestration-LangGraph-FF6F00.svg?style=flat-square&logo=langchain&logoColor=white)](https://langchain.com/)
[![Deep Agents SDK](https://img.shields.io/badge/framework-Deep%20Agents%20SDK-10B981.svg?style=flat-square)](https://docs.langchain.com/)
[![TypeSafe Jev](https://img.shields.io/badge/router-Jev%20TypeSafe%20%3C70ms-0284C7.svg?style=flat-square)](https://github.com/talkops-ai/opscloud)
[![MCP Ready](https://img.shields.io/badge/MCP-Model%20Context%20Protocol-009688.svg?style=flat-square)](https://modelcontextprotocol.io/)
[![Textual TUI](https://img.shields.io/badge/TUI-Textual-7C3AED.svg?style=flat-square)](https://textual.textualize.io/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg?style=flat-square)](LICENSE)

[Quickstart](#quickstart) • [How It Works](#what-is-opscloud) • [Plugin Marketplace](#cloud-operations-via-the-plugin-marketplace) • [Approval Modes](#safety--human-in-the-loop-governance) • [Jev System One](#jev-system-one-fast-classification--routing) • [Documentation](#documentation)

</div>

---

## Terminal Interface

<p align="center">
  <img src="docs/assets/demo.gif" alt="OpsCloud Terminal Interface" width="100%">
</p>

---

## What is OpsCloud?

OpsCloud brings two essential engineering capabilities together into a single terminal agent that anyone on your team can use:

1. **A Hands-On Coding Agent**: Powered by the main Deep Agent and its built-in platform skills. OpsCloud reads your codebase, writes new features, fixes bugs, generates Infrastructure as Code (Terraform, OpenTofu, CDK), authors Kubernetes manifests, and builds CI/CD pipelines. It follows a safe, responsible principle: **produce reviewable diffs and plans, never blind unreviewed deployments**.
2. **Autonomous Cloud Operations**: Instead of locking you into a monolithic set of tools, OpsCloud connects directly to the **TalkOps DevOps Plugin Marketplace** ([`talkops-ai/devops-plugins`](https://github.com/talkops-ai/devops-plugins)). With a single command, you can install specialized AI agents (like SRE, FinOps, Cloud Security, and Database engineers) or domain skill packs across AWS, Azure, GCP, and Kubernetes to inspect live environments, diagnose incidents, and optimize cloud infrastructure.

Whether you are writing code or troubleshooting live cloud systems, OpsCloud gives you an intelligent terminal assistant that plans carefully, uses tools safely, and always keeps you in control.

---

## Quickstart

### 1. Installation

Install OpsCloud using the automated installation script:

```bash
curl -LsSf https://opscloud.talkops.ai/install.sh | bash
```

> [!NOTE]
> On Windows, run OpsCloud inside **[WSL (Windows Subsystem for Linux)](https://learn.microsoft.com/en-us/windows/wsl/install)** for complete terminal, shell, and TUI compatibility.

### 2. Launch and Authentication

Start the interactive terminal UI:

```bash
opscloud
```

Configure your LLM provider credentials interactively with `/auth`:

```text
/auth
```

Supported providers include Anthropic, AWS Bedrock, OpenAI, Google Gemini, Azure OpenAI, Mistral, Ollama, and 15+ others. Credentials can also be exported in your shell:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
# or: export AWS_REGION="us-east-1"
# or: export OPENAI_API_KEY="sk-..."
```

### 3. Run a Task

Type any coding or cloud objective directly into the prompt:

```text
Audit unused EBS volumes and idle NAT Gateways across us-east-1 and us-west-2, calculate monthly cost impact, and draft Terraform deletion diffs
```

OpsCloud inspects existing infrastructure, plans the execution, offloads raw cloud telemetry to the filesystem, and presents structured findings with actionable remediation steps.

---

## The Two Core Pillars

### Pillar 1: Built-in Coding Agent (Deep Agent + Platform Skills)

OpsCloud acts as an expert software and DevOps engineer directly in your terminal:

- **Infrastructure as Code (IaC)**: Authors and refactors production-grade Terraform (HCL), OpenTofu, Terragrunt, AWS CDK (TypeScript and Python), CloudFormation, and Pulumi. Enforces modular architecture, state locking, provider version pinning, and dry-run validation.
- **Containers & Kubernetes**: Generates and patches Kubernetes manifests (Deployments, StatefulSets, Ingress, NetworkPolicies, CRDs), Helm charts, Kustomize overlays, Dockerfiles, and compose configurations with non-root security contexts and resource limits.
- **CI/CD & GitOps Automation**: Builds and fixes GitHub Actions workflows, GitLab CI/CD pipelines, ArgoCD Application manifests, and Tekton pipelines with pinned actions and secret masking.
- **Platform Tooling & Automation**: Writes robust bash scripts, Python platform utilities (boto3, click, typer), Makefiles, and operational CLI tools.
- **Policy as Code & Observability**: Authors OPA/Rego policies, Kyverno rules, Prometheus alert specifications, Datadog/CloudWatch monitor definitions, and Grafana dashboard JSON models.

### Pillar 2: Cloud Operations via the Plugin Marketplace

OpsCloud connects directly to the **TalkOps DevOps Plugin Marketplace** ([`talkops-ai/devops-plugins`](https://github.com/talkops-ai/devops-plugins)). The marketplace provides three types of plugins:

#### 1. Agent Plugins (Specialist Subagents)
Self-contained autonomous subagents that the root orchestrator delegates to automatically based on task intent. Each agent includes its own system prompt, isolated memory store, and dedicated MCP tools:
- **`aws-finops-agent`**: Audits cloud spend, identifies idle resources, checks savings plans, and writes cost-optimization diffs.
- **`aws-sre-agent`**: Investigates CloudWatch alarms, traces distributed errors with X-Ray, analyzes logs, and pinpoints root causes.
- **`aws-iac-engineer`**: Architects, validates, and deploys CDK, CloudFormation, and Terraform infrastructure.
- **`aws-cloud-security-engineer`**: Audits IAM policies, inspects Security Hub/GuardDuty findings, and remediates vulnerabilities.
- **`aws-database-engineer`**: Manages migrations, tunes queries, and provisions Aurora, DynamoDB, and RDS instances.
- **`aws-platform-engineer`**: Manages EKS clusters, ECS services, VPC networking, and edge routing.

#### 2. Vertical Plugins (Domain Skill Bundles)
Domain packs that attach directly to the main agent to provide immediate expertise and MCP connectors without spawning a separate subagent (e.g. `aws-networking`, `aws-containers`, `aws-cost-optimization`, `aws-observability`, `aws-security-identity`).

#### 3. Partner-Built Plugins
Official vendor-maintained skills, such as HashiCorp's official Terraform skill collection.

#### Managing Plugins

Add the marketplace and install plugins with simple CLI commands:

```bash
# Add the TalkOps marketplace
opscloud plugin marketplace add talkops-ai/devops-plugins

# Install specialist agents
opscloud plugin install aws-sre-agent
opscloud plugin install aws-finops-agent

# Install vertical domain skill packs
opscloud plugin install aws-networking

# List installed and available plugins
opscloud plugin list
```

---

## Dynamic Subagent Architecture

OpsCloud does not rely on rigid, hardcoded subagents bundled in the binary. Instead, subagents are discovered dynamically from four sources:

1. **Agent Plugins**: Installed from the plugin marketplace, bundling domain prompts, scoped skills, and isolated MCP servers.
2. **Project Definitions**: Placed in `.opscloud/agents/` or `.agents/` inside your Git repository.
3. **User Definitions**: Configured in `~/.opscloud/agents/` or `~/.agents/` on your local workstation.
4. **Async Remote Subagents**: Background agents declared in `config.toml` under `[async_subagents]`.

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

### Key Subagent Capabilities
- **SubagentMemoryStore**: Subagents run within an isolated memory sandbox. Heavy cloud searches, compiler logs, and lint iterations stay inside the subagent so your main conversation thread remains clean and focused.
- **Dual-Level Context Compaction**: `CLICompactionMiddleware` runs on both the root orchestrator and individual subagents. When conversation history grows large, earlier context is summarized and offloaded to disk while keeping execution active.
- **Live TUI Subagent Panel**: The Textual interface includes a dedicated dock showing running subagents, active tools, turn timing, and cumulative token costs.
- **System Tool Whitelist**: Tools like `compact_conversation` and `ask_user` remain accessible to subagents regardless of tool filtering rules.

---

## Safety & Human-in-the-Loop Governance

OpsCloud enforces strict human-in-the-loop controls to prevent accidental modifications to production environments.

```
┌────────────────────────────────────────────────────────────────────────┐
│                        OpsCloud Security Architecture                  │
├────────────────────────────────────────────────────────────────────────┤
│  User Request ──> Unicode & Shell Scanner ──> Approval Mode Evaluator  │
│                                                │                       │
│            ┌───────────────────┬───────────────┴────────────────┐      │
│            ▼                   ▼                                ▼      │
│     [Manual Mode]         [Auto Mode]                     [Smart Mode] │
│   Prompts human for    Classic LLM classifier          TypeSafe AI Jev │
│   every mutating tool  evaluates actions               System One gate │
│   confirmation         via primary model               (<100ms audit)  │
│            │                   │                                │      │
│            └───────────────────┴───────────────┬────────────────┘      │
│                                                ▼                       │
│                           Headless MCP Guard (4 Security Tiers)        │
│                           [READ_ONLY | MUTATING_SAFE | PRIVILEGED]     │
│                                                │                       │
│                                                ▼                       │
│                           "Produce Diffs, Not Deployments" (IaC Gate)  │
│                                                │                       │
│                                                ▼                       │
│                           Workspace & Cloud Execution                  │
└────────────────────────────────────────────────────────────────────────┘
```

### Approval Modes

OpsCloud supports three approval modes:

| Mode | Identifier | CLI Flag | Description |
|---|---|---|---|
| **Manual** | `manual` | Default | Prompts for confirmation before every mutating or risky action. Presents `[Approve]`, `[Reject]`, `[Edit Command]`, and `[Always Allow]` options. Safe default for production environments. |
| **Auto** | `auto` | `-y`, `--auto-approve` | Classic classifier mode. Uses the primary LLM reasoning model to evaluate action safety. Default for non-interactive/headless executions (`-p` / `--prompt`). |
| **Smart** | `smart` | `--smart` | Evaluates safety via the **TypeSafe AI Jev System One** classifier in <100ms. Calculates calibrated mutation probabilities and blast radius without LLM generation latency. |

Cycle approval modes at runtime inside the interactive TUI using **`Shift+Tab`**:

```text
Manual ──> Auto ──> Smart ──> Manual
```

### Multi-Layer Security Checks
- **Shell AST Scanner**: Parses shell commands prior to execution to detect dangerous patterns and enforce allowlists (`-S recommended`, `-S all`, or custom CSV).
- **Unicode Security Scanner**: Screens input and source files for Trojan Source attacks, bidirectional text overrides, and homoglyphs.
- **SSRF Guard**: Blocks outbound requests to cloud metadata endpoints (`169.254.169.254`), localhost, and private RFC-1918 subnets.
- **Headless MCP Guard**: Categorizes MCP tools into security tiers (`READ_ONLY`, `MUTATING_SAFE`, `MUTATING_DESTRUCTIVE`, `PRIVILEGED`) to enforce minimum privilege during unattended execution.

---

## Jev System One: Fast Classification & Routing

OpsCloud integrates TypeSafe AI Jev System One as an ultra-fast semantic classification and routing layer across three core functions:

### 1. Smart Approval Mode (<100ms Safety Gate)
In Smart mode, `JevSecurityClassifier` evaluates proposed tool calls before execution. Rather than waiting for a multi-second LLM inference call, Jev scores:
- `mutating_probability`: Likelihood that the command will alter files, cloud state, or infrastructure.
- `blast_radius`: Extent of affected infrastructure (single resource vs. cluster/vpc-wide).
- `risk_level`: Calibrated tier (0 = Safe, 1 = Controlled/Reversible, 2 = Critical).
- `requires_human_interrupt`: Deterministic decision whether to pause for operator approval.

### 2. Dynamic Model Router Middleware (<70ms)
When running in Smart mode, `JevDynamicModelRouterMiddleware` evaluates incoming prompt complexity in under 70ms and dynamically selects the optimal model tier:
- **Fast Tier**: Read-only inspections, syntax linting, git status, and straightforward file edits.
- **Standard Tier**: General DevOps coding, manifest authoring, and Terraform module creation.
- **Powerful Tier**: Multi-file refactoring, distributed architecture changes, and deep incident root-cause analysis.

### 3. Jev Rubric Evaluation & Compiler
The rubric evaluation framework (`JevHybridRubricGrader` and `JevCriteriaCompiler`) provides a two-tier verification mechanism:
- **Tier 1 (Jev System One Fast-Pass)**: Evaluates acceptance criteria against task evidence in parallel in under 200ms.
- **Tier 2 (Frontier LLM Fallback)**: If criteria fail, invokes a frontier model specifically to diagnose remediation steps and feed them back to the working agent.

---

## CI/CD Rubric Grading Loop

For automated verification in CI/CD pipelines, OpsCloud supports closed-loop rubric grading:

```bash
opscloud -p "Create an AWS EKS Cluster Autoscaler Helm values configuration" \
  --rubric "1. AWS IAM role ARN is referenced via serviceAccount annotation.
2. Balance-similar-node-groups flag is enabled.
3. Expander strategy is set to least-waste.
4. Scale-down-utilization-threshold is configured.
5. Resource requests and limits are explicitly defined." \
  --rubric-model "anthropic:claude-3-5-sonnet-20241022" \
  --rubric-max-iterations 3 \
  --smart
```

```
┌────────────────────────────────────────────────────────┐
│                   Rubric Evaluation Loop               │
├────────────────────────────────────────────────────────┤
│ 1. Worker Agent creates code/manifests in workspace    │
│ 2. Grader evaluates work tree against rubric criteria  │
│ 3. If PASS ──> Exit 0, emit JSON verification report   │
│ 4. If FAIL ──> Grader injects actionable remediation   │
│    deficiency report into Worker Agent context         │
│ 5. Worker iterates on fixes and re-submits to Grader   │
│ 6. Repeats until PASS or max iterations reached        │
└────────────────────────────────────────────────────────┘
```

---

## Remote Cloud Sandboxes

For isolating untrusted workloads or heavy builds, OpsCloud supports execution within ephemeral cloud sandboxes via `--sandbox`:

```bash
opscloud --sandbox modal "Compile and test the cross-platform platform binary"
```

Supported sandbox providers include Modal, Daytona, LangSmith, Runloop, AgentCore, and local Docker containers. Workspace files synchronize bi-directionally, returning diffs and build artifacts upon completion.

---

## Documentation

Comprehensive documentation is available in [`docs/opscloud-docs/`](docs/opscloud-docs/):

| Guide | Description |
|---|---|
| **[Overview](docs/opscloud-docs/overview.md)** | Core capabilities, execution engines, architecture, and environment configuration |
| **[Quickstart](docs/opscloud-docs/quickstart.md)** | Step-by-step setup, interactive TUI controls, credential setup, and first steps |
| **[CLI Reference](docs/opscloud-docs/cli-reference.md)** | Command-line options, subcommands, flags, and slash commands |
| **[Configuration](docs/opscloud-docs/Configuration.md)** | Configuration hierarchy, directory layouts, and runtime environment options |
| **[config.toml Reference](docs/opscloud-docs/config.toml.md)** | Full configuration specification: model pools, UI, permissions, and compaction |
| **[Provider Credentials](docs/opscloud-docs/credentials.md)** | Credential setup for 22+ providers via `/auth` and environment variables |
| **[Approval Modes & Security](docs/opscloud-docs/approval-mode.md)** | Manual, Auto, and Smart modes, Jev System One classifier, and AST scanners |
| **[Subagents](docs/opscloud-docs/subagents.md)** | Dynamic subagent discovery, isolated memory stores, and context compaction |
| **[Memory & Skills](docs/opscloud-docs/memory-and-skills.md)** | Workspace memory persistence, skill hierarchy, and convention learning |
| **[MCP Tools](docs/opscloud-docs/mcp-tools.md)** | Model Context Protocol configuration, security tiers, and TUI inspector |
| **[Plugins & Marketplaces](docs/opscloud-docs/plugins.md)** | Community and private enterprise plugin architecture and discovery |
| **[Lifecycle Hooks](docs/opscloud-docs/hooks.md)** | Deterministic pre/post-tool execution scripts via `hooks.json` |
| **[Model Providers & Router](docs/opscloud-docs/model-providers.md)** | Model providers, Jev dynamic model routing (<70ms), and reasoning effort |
| **[Goals & Rubrics](docs/opscloud-docs/goal-and-rubrics.md)** | Interactive goal tracking and autonomous CI/CD rubric grading loops |
| **[Remote Sandboxes](docs/opscloud-docs/remote-sandboxes.md)** | Cloud sandbox execution with Modal, Daytona, and Docker |

---

## CLI Reference Summary

```bash
# Basic Usage
opscloud [OPTIONS] [PROMPT]

# Common Subcommands
opscloud auth list | set <provider> | remove <provider>
opscloud config show | list | get <key> | set <key> <value>
opscloud pool show | set <tier> <model> | reset
opscloud plugin list | install <id> | uninstall <id> | marketplace add <url>
opscloud skills list | info <name> | find <query> | create <name>
opscloud mcp list | tools | test <server>
opscloud threads list | delete <id>
opscloud agents list | reset --agent <name>
opscloud doctor

# Key Flags
-p, --prompt TEXT                # Headless single-task execution prompt
-r, --resume [ID]                # Resume a previous conversation thread
-M, --model MODEL                # Primary model specifier (provider:model)
-a, --agent NAME                 # Launch with a specific dynamic subagent
-s, --skill NAME                 # Pre-load a specific skill
--approval-mode MODE             # Approval mode: manual, auto, or smart
-y, --auto-approve               # Enable classic classifier-backed Auto mode
--smart                          # Enable Jev-powered Smart approval mode (<100ms)
-S, --shell-allow-list LIST      # Shell allowlist (recommended, all, or custom CSV)
--effort LEVEL                   # Reasoning effort (off, low, medium, high)
--goal TEXT                      # Interactive goal with acceptance criteria
--rubric TEXT|@PATH              # Autonomous rubric grading loop
--rubric-model MODEL             # Grader model for rubric evaluation
--rubric-max-iterations N        # Max grading iterations
--sandbox [TYPE]                 # Ephemeral cloud sandbox provider
```

---

## Contributing

Contributions are welcome. Please review our [Contributing Guidelines](CONTRIBUTING.md) and [Security Policy](SECURITY.md) before submitting a pull request.

```bash
git clone https://github.com/talkops-ai/opscloud.git
cd opscloud
uv venv && source .venv/bin/activate
uv pip install -e ".[dev,test-integration]"
uv run pytest tests/unit_tests/ -v
```

---

## License

OpsCloud is open-source software licensed under the [Apache License 2.0](LICENSE).
