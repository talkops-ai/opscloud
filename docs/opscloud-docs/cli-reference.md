# CLI Reference

> Complete command-line flags, subcommands, and slash commands for OpsCloud

## Usage

```bash
opscloud [OPTIONS] [PROMPT]
```

When run without arguments, OpsCloud starts in interactive TUI mode. Pass a positional prompt or use `-n` for non-interactive headless execution.

---

## Command-line options

### Prompts and input

| Flag | Description |
|---|---|
| `PROMPT` | Non-interactive prompt (positional argument, alternative to `-n`) |
| `-n`, `--non-interactive TEXT` | Run a single task non-interactively and exit |
| `-m`, `--message TEXT` | Initial prompt to auto-submit when an interactive session launches |
| `-s`, `--skill NAME` | Pre-load a specific skill into context at startup |
| `--startup-cmd CMD` | Shell command executed at startup before the first prompt |
| `--stdin` | Read input explicitly from standard input |

### Thread management

| Flag | Description |
|---|---|
| `-r`, `--resume [ID]` | Resume a thread: `-r` for most recent, `-r <ID>` for a specific thread ID |

### Agent selection

| Flag | Description |
|---|---|
| `-a`, `--agent NAME` | Subagent to launch directly (e.g. `eks-cluster-operator`) |

### Model and router profile

| Flag | Description |
|---|---|
| `-M`, `--model MODEL` | Model specifier overriding defaults (e.g., `anthropic:claude-3-7-sonnet-20250219`, `openai:gpt-4o`) |
| `--effort LEVEL` | Set reasoning effort level: `off`, `low`, `medium`, `high` |
| `--pool TIER` | Force a specific Jev router pool tier: `fast`, `standard`, `powerful` |
| `--model-params JSON` | Extra kwargs to pass to the model constructor as a JSON string |
| `--max-retries N` | Override max retries for transient provider API errors |
| `--profile-override JSON` | Override model profile fields as a JSON string |
| `--default-model [MODEL]` | Set or inspect the persistent default model |
| `--clear-default-model` | Clear the configured persistent default model |

### Non-interactive mode controls

These flags require `-n`, a positional prompt, or piped stdin:

| Flag | Description |
|---|---|
| `-q`, `--quiet` | Clean output suitable for piping stdout (suppresses spinners and banners) |
| `--no-stream` | Buffer the full response before writing to stdout |
| `--max-turns N` | Maximum agentic turns before stopping |
| `--timeout SECONDS` | Hard wall-clock timeout in seconds |

### Goal and rubric evaluation

| Flag | Description |
|---|---|
| `--goal TEXT` | High-level objective to generate interactive acceptance criteria |
| `--rubric TEXT\|@PATH` | Acceptance criteria text or `@path` to a file for autonomous CI/CD grading |
| `--rubric-model MODEL` | Dedicated grader model for rubric self-evaluation (e.g., `openai:gpt-4o`) |
| `--rubric-max-iterations N` | Maximum fix-and-recheck iterations before failing |
| `--recursion-limit N` | Override main agent recursion depth limit (default: 2000) |

### Approval modes

Mutually exclusive:

| Flag | Description |
|---|---|
| `--approval-mode MODE` | Explicit tool approval mode: `manual`, `auto`, or `smart` (TypeSafe AI Jev) |
| `-y`, `--auto-approve` | Enable classic classifier-backed Auto mode (evaluates actions with primary LLM) |
| `--smart` | Enable Jev-powered Smart approval mode (<100ms System One safety gate) |

### Shell safety and sandboxes

| Flag | Description |
|---|---|
| `-S`, `--shell-allow-list LIST` | Comma-separated allowed shell commands (`recommended`, `all`, or custom CSV list) |
| `--sandbox [TYPE]` | Run within an ephemeral cloud sandbox (`agentcore`, `daytona`, `langsmith`, `modal`, `runloop`, `vercel`; default: `none`) |
| `--sandbox-id ID` | Attach to an existing running sandbox container |
| `--sandbox-snapshot-name NAME` | Snapshot or blueprint name to attach or create |
| `--sandbox-setup PATH` | Path to setup shell script executed immediately after sandbox provisioning |

### Interpreter and filesystem tools

| Flag | Description |
|---|---|
| `--interpreter` / `--no-interpreter` | Toggle JavaScript QuickJS interpreter (`js_eval`) middleware |
| `--interpreter-tools VALUE` | Programmatic Tool Calling (PTC) allowlist: `safe`, `all`, or comma-separated list |
| `--allow-fs-tools LIST` | Allowlist of filesystem tools (`all` or comma-separated list) |

### MCP and security

| Flag | Description |
|---|---|
| `--mcp-config PATH` | Path to explicit MCP JSON configuration file |
| `--no-mcp` | Disable all MCP tool loading |
| `--trust-project-mcp` | Skip interactive approval for project-level MCP configurations |
| `--trust-project-hooks` | Trust project-level `.opscloud/hooks.json` script handlers |
| `--acp` | Run as an Agent Client Protocol (ACP) server over stdio instead of launching the TUI |

### Meta

| Flag | Description |
|---|---|
| `-v`, `--version` | Display OpsCloud version and exit |
| `--verbose` | Enable verbose debug logging |

---

## Subcommands

### `opscloud config`

Inspect and mutate configuration without launching an interactive session:

```bash
opscloud config show          # Show all resolved configuration values and their sources
opscloud config list          # List all available configuration options with types and defaults
opscloud config get <key>     # Show effective value and source for a single option
opscloud config set <key> <v> # Set a configuration value in ~/.opscloud/config.toml
opscloud config path          # Show config file locations and existence status
```

### `opscloud auth`

Manage provider credentials directly from the shell:

```bash
opscloud auth list            # List configured credentials and their sources
opscloud auth set <provider>  # Set an API key or endpoint for a provider
opscloud auth remove <provider> # Remove a stored credential
```

### `opscloud pool`

Manage the Jev TypeSafe Dynamic Model Router pools:

```bash
opscloud pool show            # Display active Fast, Standard, and Powerful model assignments
opscloud pool set <tier> <m>  # Assign a model to a tier (e.g., opscloud pool set fast anthropic:claude-3-5-haiku-latest)
opscloud pool reset           # Reset model pools to system defaults
```

### `opscloud plugin`

Manage plugins and marketplace sources:

```bash
opscloud plugin list          # List installed and project plugins
opscloud plugin install <id>  # Install a plugin from a marketplace
opscloud plugin uninstall <id># Uninstall an installed plugin
opscloud plugin enable <id>   # Enable a disabled plugin
opscloud plugin disable <id>  # Disable an installed plugin
opscloud plugin marketplace add <url>     # Add a remote or local marketplace source
opscloud plugin marketplace remove <name> # Remove a marketplace source
opscloud plugin marketplace list          # List active marketplace sources
```

### `opscloud skills`

Inspect and scaffold skills:

```bash
opscloud skills list          # List all discovered skills across all resolution sources
opscloud skills info <name>   # Display details, domain, and description for a skill
opscloud skills find <query>  # Search skills by keyword
opscloud skills create <name> # Scaffold a new skill directory with SKILL.md
```

### `opscloud mcp`

Inspect and test MCP server connections:

```bash
opscloud mcp list             # List configured MCP servers
opscloud mcp tools            # List available tools across all MCP servers
opscloud mcp test <server>    # Test connection and tool registration for a server
```

### `opscloud threads`

Manage persistent conversation checkpoints:

```bash
opscloud threads list         # List recent conversation threads
opscloud threads delete <id>  # Delete a specific thread checkpoint
```

### `opscloud agents`

Inspect and manage domain subagents:

```bash
opscloud agents list          # List built-in, user, and project subagents
opscloud agents reset --agent <name> # Reset a subagent's prompt to default
```

### `opscloud doctor`

Run comprehensive system diagnostics, verifying AWS STS credentials, Kubernetes context, LLM endpoints, core tools, and local dependencies.

---

## Interactive slash commands

Available inside an interactive TUI session. Type `/help` to see the full list.

### Core commands

| Command | Aliases | Description |
|---|---|---|
| `/auth` | `/login` | Open interactive credential manager |
| `/logout` | — | Remove stored credentials |
| `/model` | — | Open model selector modal |
| `/pool` | — | Switch between Jev Dynamic Router tiers |
| `/effort` | — | Set reasoning effort (`off`, `low`, `medium`, `high`) |
| `/fast` | — | Quick switch to the fast routing tier |
| `/config` | — | View or modify runtime configuration |
| `/permissions` | `/perms` | Inspect and toggle tool and shell permissions |
| `/skills` | — | Browse and inspect active skills |
| `/mcp` | — | Open the interactive MCP Server and Tool viewer |
| `/plugins` | — | Manage plugins and marketplace sources |
| `/cost` | — | View session token consumption and total USD cost |
| `/context` | — | View context window token utilization gauge |
| `/compact` | — | Trigger immediate conversation compaction |
| `/clear` | — | Clear conversation history |
| `/clear!` | — | Force clear history without confirmation prompt |
| `/resume` | — | Open thread selector modal to resume a past session |
| `/doctor` | — | Run system diagnostics (auth, tools, dependencies) |
| `/bug` | — | Open GitHub bug report template |
| `/help` | `/h` | List all available slash commands |
| `/exit` | `/quit` | Exit OpsCloud session |

### Power commands

| Command | Description |
|---|---|
| `/agents` | Open subagent selector modal to switch active domain subagent |
| `/goal <text>` | Define high-level objective and generate acceptance criteria |
| `/rubric <text\|@file>`| Attach evaluation rubric to current session |
| `/tasks` | Open task management board |
| `/loop` | Enter autonomous execution loop |
| `/review` | Request agent self-review of recent workspace modifications |
| `/memory` | View, save, or delete persistent memory entries (`AGENTS.md`) |
| `/btw <note>` | Send out-of-band note without triggering LLM turn execution |
| `/copy` | Copy last assistant response to system clipboard |
| `/trace` | View or toggle LangSmith tracing status |
| `/version` | Display OpsCloud version and environment metadata |
| `/reload` | Hot-reload configuration and skill definitions without restart |
| `/restart` | Restart current agent session |
| `/update` | Check for newer OpsCloud package releases |
| `/auto-update` | Toggle automatic background updates |
| `/install` | Install missing package dependencies |
| `/notifications`| Toggle desktop notification toasts |
| `/scrollbar` | Toggle chat window vertical scrollbars |
| `/timestamps` | Toggle timestamp display on messages |
| `/skill <name>` | Explicitly invoke a skill |
| `/skill-create`| Distill current conversation into a new reusable skill |

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success / Rubric Passed |
| `1` | General runtime error / Rubric Failed |
| `2` | Argument parsing or validation error |
| `130` | Interrupted by user (Ctrl+C / SIGINT) |
