# Quickstart

> Install OpsCloud, set up your model provider, and run your first task in under 2 minutes

OpsCloud is a terminal-native AI agent for full-stack software development, DevOps, and Platform Engineering. This guide covers installation, running your first task, mastering interactive mode, headless automation, and tracing.

## Install and run your first task

### 1. Install

```bash
curl -LsSf https://opscloud.talkops.ai/install.sh | bash
```

> [!NOTE]
> OpsCloud is built for macOS and Linux. Windows users should run OpsCloud inside **[WSL (Windows Subsystem for Linux)](https://learn.microsoft.com/en-us/windows/wsl/install)** for full terminal TUI and toolchain compatibility.

### 2. Launch the TUI

```bash
opscloud
```

### 3. Configure credentials

Inside the interactive TUI, type:

```text
/auth
```

Select your provider (Anthropic, OpenAI, Google, AWS Bedrock, etc.) and enter your API key. Keys are encrypted and saved to `~/.opscloud/.env`. See [Credentials](./credentials.md) for alternative setup methods.

> [!TIP]
> For web search capabilities, OpsCloud uses [Tavily](https://tavily.com). Add your key via `/auth` or export `TAVILY_API_KEY="tvly-..."`.

### 4. Give it a task

Type a prompt directly into the TUI chat input:

```text
Create a Terraform module for an AWS S3 bucket with KMS customer-managed encryption, versioning, and lifecycle policies, plus a Python FastAPI endpoint that uploads files to it
```

OpsCloud routes the prompt using the **Jev Dynamic Router**, selects the appropriate subagent, and presents a syntax-highlighted diff for your approval before executing any file writes or commands.

### 5. Enable tracing (optional)

Run `/auth` and add your LangSmith API key. Tracing starts on the next launch.

---

## Interactive mode

Type naturally in the chat window. OpsCloud uses its built-in tools, domain subagents, and memory to complete complex workflows.

### Slash commands

| Command | Aliases | What it does |
|---|---|---|
| `/auth` | `/login` | Manage LLM provider credentials and base URLs |
| `/model` | — | Switch models or open the interactive model picker |
| `/pool` | — | Switch between Jev Dynamic Router tiers (`fast`, `standard`, `powerful`) |
| `/effort` | — | Adjust reasoning effort level (`off`, `low`, `medium`, `high`) |
| `/fast` | — | Quick switch to the fast routing tier |
| `/agents` | — | Switch between active domain subagents |
| `/skills` | — | Browse and inspect loaded skills |
| `/memory` | — | View, save, or delete persistent memory entries (`AGENTS.md`) |
| `/mcp` | — | Open the interactive MCP Server and Tool viewer |
| `/plugins` | — | Manage installed plugins and marketplace sources |
| `/config` | — | View or modify runtime configuration settings |
| `/permissions` | `/perms` | Inspect and toggle tool and shell permissions |
| `/cost` | — | View session token consumption and total USD cost |
| `/context` | — | View context window token utilization gauge |
| `/compact` | — | Trigger immediate conversation context compaction |
| `/goal` | — | Set a high-level goal with tracking acceptance criteria |
| `/rubric` | — | Attach or inspect evaluation rubric criteria |
| `/tasks` | — | Open the agent's internal task tracking board |
| `/review` | — | Request self-review of recent workspace modifications |
| `/loop` | — | Enter an autonomous execution loop |
| `/copy` | — | Copy the last assistant response to clipboard |
| `/clear` | — | Clear visual conversation history |
| `/resume` | — | Browse and resume previous threads |
| `/trace` | — | View or toggle LangSmith tracing status |
| `/doctor` | — | Run system health diagnostics (auth, tools, dependencies) |
| `/bug` | — | Open GitHub bug report template |
| `/btw` | — | Send an out-of-band note without triggering a new agent turn |
| `/help` | `/h` | List all available slash commands |
| `/exit` | `/quit` | Exit OpsCloud |
| `/version` | — | Show version and environment metadata |

### Keyboard shortcuts

| Shortcut | What it does |
|---|---|
| `Shift+Tab` | Cycle approval mode: **Manual** → **Auto** → **Smart** |
| `Escape` | Cancel current response stream |
| `Ctrl+C` | Interrupt a running tool or exit the application |

### Resume a conversation

OpsCloud automatically saves sessions as SQLite thread checkpoints. Pick up where you left off:

```bash
opscloud -r              # Resume the most recent thread
opscloud -r <thread-id>  # Resume a specific thread ID
```

Or run `/resume` inside a session to browse past threads visually.

### Switch subagents

Start a session with a specific subagent:

```bash
opscloud -a eks-cluster-operator
```

Or switch dynamically inside the TUI with `/agents`. See [Subagents](./subagents.md).

---

## Non-interactive / headless mode

Run single tasks headlessly from terminal scripts or CI/CD pipelines using `-n`:

```bash
opscloud -n "Audit and validate all Terraform modules in this repository"
```

### Pipe input

Pipe logs, error traces, or manifests directly into OpsCloud:

```bash
cat pod-spec.yaml | opscloud -n "Review this pod spec for Pod Security Standard violations"
git diff | opscloud -n "Generate conventional commit message and release notes"
```

### Output control flags

| Flag | Description |
|---|---|
| `-q`, `--quiet` | Clean stdout output suitable for UNIX pipelines (suppresses banners and spinners) |
| `--no-stream` | Buffer the full response before outputting |
| `--max-turns N` | Limit the maximum number of agentic execution turns |
| `--timeout SECONDS` | Enforce a hard wall-clock execution timeout |

### Self-evaluation with rubrics

Pair the working agent with an autonomous grader model in CI/CD:

```bash
opscloud -n "Generate production Kubernetes deployment manifests for the auth service" \
  --rubric "1. Non-root securityContext is enforced.
2. Resource requests and limits are defined.
3. Liveness and readiness probes have initialDelaySeconds configured." \
  --rubric-model "openai:gpt-4o" \
  --rubric-max-iterations 3 \
  -y
```

See [Goals and Rubrics](./goal-and-rubrics.md) for full CI/CD specifications.

---

## Trace with LangSmith

OpsCloud integrates natively with [LangSmith](https://smith.langchain.com) for tracing agent turn execution, subagent delegation, and tool invocations.

1. Configure your key via `/auth` or set `export LANGSMITH_API_KEY="ls-..."`.
2. Optionally specify a project name:
   ```bash
   export LANGSMITH_PROJECT="opscloud-production"
   opscloud
   ```
3. Tracing activates automatically upon detection.

---

## What's next

- **[CLI Reference](./cli-reference.md)** — Complete options, flags, and subcommand reference.
- **[Configuration](./Configuration.md)** — `config.toml`, environment variables, and directory layout.
- **[Approval Modes](./approval-mode.md)** — Manual, Auto, and Smart modes with safety scanners.
- **[Subagents](./subagents.md)** — Multi-agent delegation and context compaction.
- **[MCP Tools](./mcp-tools.md)** — Connecting external tools via Model Context Protocol.
