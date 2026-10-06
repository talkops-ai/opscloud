# CLI Reference

> Complete command-line flags, subcommands, and slash commands for OpsCloud

## Usage

```bash
opscloud [OPTIONS] [PROMPT]
```

When run without arguments, OpsCloud starts in interactive TUI mode. Pass a positional prompt or use `-p` / `-n` for non-interactive headless execution.

---

## Command-Line Options

### Input & Headless Execution

| Flag | Description |
|---|---|
| `PROMPT` | Non-interactive task prompt (positional argument) |
| `-p`, `--prompt`, `-n`, `--non-interactive TEXT` | Run a single task non-interactively and exit |
| `-m`, `--message`, `--initial-prompt TEXT` | Initial prompt to auto-submit when interactive session launches |
| `-s`, `--skill NAME` | Pre-load a specific skill into context at startup |
| `--startup-cmd CMD` | Shell command executed at startup before the first prompt |
| `--stdin` | Read input explicitly from standard input (supports piped scripts) |

### Thread Management

| Flag | Description |
|---|---|
| `-r`, `--resume [ID]` | Resume a thread: `-r` for most recent, `-r <ID>` for a specific thread ID |

### Dynamic Agent Selection

| Flag | Description |
|---|---|
| `-a`, `--agent NAME` | Launch directly with a specific subagent (e.g. `aws-sre-agent`, `aws-iac-engineer`) |

### Model & Routing

| Flag | Description |
|---|---|
| `-M`, `--model MODEL` | Model specifier (e.g., `anthropic:claude-3-7-sonnet-20250219`, `openai:gpt-4o`) |
| `--effort LEVEL` | Set reasoning effort level: `off`, `low`, `medium`, `high` |
| `--pool TIER` | Force a specific Jev router pool tier: `fast`, `standard`, `powerful` |
| `--model-params JSON` | Extra kwargs to pass to model constructor as JSON string (e.g. `'{"temperature": 0.0}'`) |
| `--profile-override JSON` | Override model profile fields as a JSON string |
| `--default-model [MODEL]` | Set or inspect the persistent default model for future launches |
| `--clear-default-model` | Clear configured default model |

### Approval Modes & Safety

| Flag | Description |
|---|---|
| `--approval-mode MODE` | Explicit tool approval mode: `manual`, `auto`, or `smart` (TypeSafe AI Jev) |
| `-y`, `--auto-approve` | Enable classic classifier-backed Auto mode (evaluates actions with primary LLM) |
| `--smart` | Enable Jev-powered Smart approval mode (<100ms System One safety gate) |
| `-S`, `--shell-allow-list LIST` | Comma-separated allowed shell commands (`recommended`, `all`, or custom CSV) |
| `--read-only`, `--dry-run` | Enforce read-only safety mode (blocks all mutating AWS/K8s/Terraform calls) |

### Non-Interactive Controls

These flags require `-p` / `-n`, a positional prompt, or piped stdin:

| Flag | Description |
|---|---|
| `-q`, `--quiet` | Clean output suitable for piping stdout — only final assistant text goes to stdout |
| `--no-stream` | Buffer response and write to stdout at once instead of streaming chunks |
| `--json` | Output structured JSON with tool executions, timing, and token metrics |
| `--max-turns N` | Maximum agentic turns before stopping (>= 1) |
| `--timeout SECONDS` | Hard wall-clock timeout in seconds (exits 124 on expiry) |
| `--recursion-limit N` | Override main agent's LangGraph recursion step budget (default: 2000) |

### Goal & Rubric Evaluation

| Flag | Description |
|---|---|
| `--goal TEXT` | Initial goal objective to generate interactive acceptance criteria (TUI mode) |
| `--rubric TEXT\|@PATH` | Acceptance criteria text or `@path` to a file for autonomous CI/CD verification |
| `--rubric-model MODEL` | Dedicated grader model for rubric self-evaluation (e.g., `openai:gpt-4o`) |
| `--rubric-max-iterations N` | Maximum evaluation iterations for the rubric grader before failing |

### Remote Cloud Sandboxes

| Flag | Description |
|---|---|
| `--sandbox [TYPE]` | Run within an ephemeral cloud sandbox (`agentcore`, `daytona`, `langsmith`, `modal`, `runloop`, `vercel`, `docker`) |
| `--sandbox-id ID` | Attach to an existing running sandbox container |
| `--sandbox-snapshot-name NAME` | Snapshot or blueprint name to attach or create |
| `--sandbox-setup PATH` | Path to setup shell script executed immediately after sandbox provisioning |

### Model Context Protocol (MCP)

| Flag | Description |
|---|---|
| `--mcp-config PATH` | Path to custom MCP servers JSON configuration file |
| `--no-mcp` | Disable all MCP tool loading |
| `--trust-project-mcp` | Skip interactive confirmation prompts for project-level MCP servers |

### Environment & Cloud Overrides

| Flag | Description |
|---|---|
| `--cwd PATH` | Override working directory path for this execution |
| `--aws-profile PROFILE` | AWS profile override for this session |
| `--aws-region REGION` | AWS region override for this session |

### Logging & Diagnostics

| Flag | Description |
|---|---|
| `-v`, `--verbose` | Enable verbose logging |
| `--log-level LEVEL` | Set logging level: `debug`, `info`, `warning`, `error`, `critical` |
| `--log-dir PATH` | Directory for log files (default: `~/.opscloud/logs/`) |
| `--log-file PATH` | Log file path or filename |

---

## Subcommands

OpsCloud includes built-in operational subcommands for terminal management:

```bash
# System & environment health diagnostics
opscloud doctor

# Show cloud and model authentication status
opscloud auth list
opscloud auth set <provider>
opscloud auth remove <provider>

# Session thread management
opscloud threads list
opscloud threads delete <thread_id>

# Plugin & Marketplace management
opscloud plugin list
opscloud plugin install <plugin_id>
opscloud plugin uninstall <plugin_id>
opscloud plugin enable <plugin_id>
opscloud plugin disable <plugin_id>
opscloud plugin marketplace add <url_or_repo>
opscloud plugin marketplace list
opscloud plugin marketplace refresh

# Dynamic model pool management (Jev router)
opscloud pool show
opscloud pool set <fast|standard|powerful> <model_spec>
opscloud pool reset

# Configuration management
opscloud config show
opscloud config list
opscloud config get <key>
opscloud config set <key> <value>
opscloud config path

# Skill management
opscloud skills list
opscloud skills info <skill_name>
opscloud skills find <query>

# Model Context Protocol (MCP) inspection
opscloud mcp list
opscloud mcp tools
```

---

## Interactive Slash Commands

Inside an interactive TUI session, type `/` to open the command palette:

| Command | Aliases | Description |
|---|---|---|
| `/auth` | `/login` | Manage provider credentials, API keys, and custom base URLs |
| `/model` | — | Switch active model or open the interactive model picker |
| `/pool` | — | Switch between Jev Dynamic Router tiers (`fast`, `standard`, `powerful`) |
| `/effort` | — | Adjust reasoning effort level (`off`, `low`, `medium`, `high`) |
| `/fast` | — | Quick switch to the fast routing tier |
| `/agents` | — | Switch between active domain subagents |
| `/skills` | — | Browse and inspect loaded skills |
| `/memory` | — | View, save, or delete persistent memory entries (`AGENTS.md`) |
| `/mcp` | — | Open the interactive MCP Server and Tool inspector |
| `/plugins` | — | Manage installed plugins and marketplace sources |
| `/config` | — | View or modify runtime configuration settings |
| `/permissions` | `/perms` | Inspect and toggle tool and shell permissions |
| `/cost` | — | View session token consumption and total USD cost |
| `/context` | — | View context window token utilization gauge |
| `/compact` | — | Trigger immediate conversation context compaction |
| `/goal` | — | Set a high-level goal with interactive acceptance criteria |
| `/rubric` | — | Attach or inspect evaluation rubric criteria |
| `/tasks` | — | Open the agent's internal task tracking board |
| `/review` | — | Request self-review of recent workspace modifications |
| `/loop` | — | Enter an autonomous execution loop |
| `/copy` | — | Copy the last assistant response to clipboard |
| `/clear` | — | Clear visual conversation history |
| `/resume` | — | Browse and resume previous threads |
| `/trace` | — | View or toggle LangSmith tracing status |
| `/doctor` | — | Run system health diagnostics (auth, tools, dependencies) |
| `/btw` | — | Send an out-of-band note without triggering a new agent turn |
| `/help` | `/h` | List all available slash commands |
| `/exit` | `/quit` | Exit OpsCloud |

---

## Interactive Keyboard Shortcuts

| Shortcut | Description |
|---|---|
| `Shift+Tab` | Cycle approval modes: **Manual** → **Auto** → **Smart** |
| `Escape` | Cancel current streaming response |
| `Ctrl+C` | Interrupt a running tool or exit the application |
| `Up / Down` | Browse chat input command history |
