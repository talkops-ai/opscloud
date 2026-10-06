# Configuration

> Configure OpsCloud with config.toml, environment variables, hooks, and CLI flags

OpsCloud stores configuration under `~/.opscloud/` (user-global) and `.opscloud/` (project-local). For the complete directory tree, session storage, and skill paths, see [Data locations](#data-locations).

The primary configuration files are:

| File | Description |
|---|---|
| **[config.toml](./config.toml.md)** | Model defaults, Jev router pools, UI themes, tool allowlists, permissions, and compaction |
| **[Environment variables](#environment-variables)** | API keys and secrets stored in `~/.opscloud/.env` or exported in the shell |
| **[hooks.json](./hooks.md)** | Lifecycle event subscriptions for audit logging, pre-commit validation, and tool guards |
| **[.mcp.json](./mcp-tools.md)** | Global and project MCP server definitions |

---

## How settings resolve

OpsCloud merges settings from multiple sources in a strictly defined precedence order.

**General options** (router tiers, interpreter limits, themes, and `config.toml` keys) resolve in this order:

1. CLI command-line flags (e.g. `--model`, `-y`)
2. `OPSCLOUD_`-prefixed environment variables
3. Canonical environment variables (when applicable)
4. Project-level `.opscloud/config.toml`
5. User-level `~/.opscloud/config.toml`
6. Built-in defaults

**Provider API keys** use a dedicated resolution order. See [Credentials](./credentials.md).

**Dotenv files** load at startup: the nearest project `.env` (walking up from the launch directory), then `~/.opscloud/.env`. Shell exports always take priority over `.env` values.

---

## Inspect configuration

The `opscloud config` subcommand inspects effective configuration values without launching an interactive session:

| Command | Description |
|---|---|
| `opscloud config show` | Resolve every option and print the effective value and source |
| `opscloud config list` | List every available option with its type, default, and valid scopes |
| `opscloud config get <key>` | Show the effective value and source for a single option |
| `opscloud config set <key> <value>` | Set a configuration value in `~/.opscloud/config.toml` |
| `opscloud config path` | Show config file locations and whether each exists |

Or use `/config` inside an interactive TUI session to view and modify settings dynamically.

---

## Environment variables

### Loading order and precedence

OpsCloud loads `.env` files at startup in this order:

1. **Project `.env`** — walks up from the current directory to find the nearest project `.env`
2. **Global `~/.opscloud/.env`** — user-level credentials (provider API keys, Tavily, LangSmith)

Shell exports always override `.env` values. The `OPSCLOUD_` prefix takes priority over canonical variable names:

```
OPSCLOUD_OPENAI_API_KEY  >  OPENAI_API_KEY  >  ~/.opscloud/.env  >  project .env
```

### Security: Blocked variables

The following environment variables are strictly **blocked** from being set via `.env` files to prevent environment tampering or privilege escalation:

`PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `TERM`, `DISPLAY`, `PYTHONPATH`, `PYTHONSTARTUP`, `PYTHONHOME`, `NODE_PATH`, `NODE_OPTIONS`, `LD_PRELOAD`, `LD_LIBRARY_PATH`, `DYLD_LIBRARY_PATH`, `DYLD_INSERT_LIBRARIES`, `HISTFILE`, `HISTSIZE`, `SSH_AUTH_SOCK`, `GPG_AGENT_INFO`, `TMPDIR`, `TEMP`, `TMP`.

### OpsCloud runtime variables

OpsCloud reads the following runtime environment variables:

| Variable | Description |
|---|---|
| `OPSCLOUD_CONFIG_DIR` | Override the global configuration root directory (default: `~/.opscloud/`) |
| `OPSCLOUD_DEBUG` | Enable verbose debug logging |
| `OPSCLOUD_DEBUG_FILE` | Path for the debug log file (default: `/tmp/opscloud_debug.log`) |
| `OPSCLOUD_LOG_LEVEL` | Override runtime logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `OPSCLOUD_AUTO_UPDATE` | Toggle automatic package updates (default: `enabled`) |
| `OPSCLOUD_COLLAPSE_PASTES` | Collapse large chat-input pastes into compact placeholders (default: `enabled`) |
| `OPSCLOUD_DISABLE_COMPACTION`| Disable background context compaction middleware |

### DevOps environment preservation

OpsCloud automatically isolates and preserves DevOps-specific environment variables across tool and subprocess executions:

| Category | Variables |
|---|---|
| **Kubernetes** | `KUBECONFIG`, `KUBE_CONTEXT` |
| **AWS** | `AWS_PROFILE`, `AWS_REGION`, `AWS_DEFAULT_REGION`, `AWS_SHARED_CREDENTIALS_FILE` |
| **GCP** | `GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_CLOUD_PROJECT`, `CLOUDSDK_CORE_PROJECT` |
| **Azure** | `AZURE_SUBSCRIPTION_ID`, `AZURE_TENANT_ID` |
| **Ansible** | `ANSIBLE_CONFIG`, `ANSIBLE_INVENTORY` |
| **Helm** | `HELM_HOME`, `HELM_REPOSITORY_CONFIG` |
| **ArgoCD** | `ARGOCD_SERVER`, `ARGOCD_AUTH_TOKEN` |
| **Terraform / OpenTofu** | `TF_CLI_CONFIG_FILE`, `TERRAGRUNT_CONFIG` |

This ensures that when the agent runs `kubectl`, `terraform`, `tofu`, `helm`, or cloud CLIs, local credentials and target clusters remain immediately accessible.

---

## Project detection

OpsCloud auto-detects the project root by walking up from the current working directory looking for project markers:

| Marker | Type |
|---|---|
| `.opscloud/` | OpsCloud project configuration root |
| `.git/` | Git repository |
| `terragrunt.hcl` | Terragrunt project |
| `Chart.yaml` | Helm chart root |
| `ansible.cfg` | Ansible automation project |
| `pyproject.toml` | Python project |
| `package.json` | Node.js / TypeScript project |
| `Makefile` | Build system |

When a project root is detected, project-level configurations (`.opscloud/`, `.env`, skills, subagents, memory) are discovered and merged with user-level settings.

---

## Data locations

### User-level (`~/.opscloud/`)

| Path | Purpose |
|---|---|
| `~/.opscloud/config.toml` | Main configuration file |
| `~/.opscloud/.env` | Global API keys and secrets (strict 0600 permissions) |
| `~/.opscloud/hooks.json` | Global lifecycle hooks |
| `~/.opscloud/.mcp.json` | Global MCP server definitions |
| `~/.opscloud/memory/` | User-scoped persistent memory entries |
| `~/.opscloud/settings.json` | User-scope settings (enabled plugins, etc.) |
| `~/.opscloud/plugins/` | Plugin storage (cache, registries, marketplaces) |

### Agent-specific (`~/.opscloud/{agent}/`)

| Path | Purpose |
|---|---|
| `~/.opscloud/{agent}/skills/` | User-level skills for a specific subagent |
| `~/.opscloud/{agent}/agents/` | User-level custom subagents |
| `~/.opscloud/{agent}/AGENTS.md` | User-level instructions and memories |

### Managed state (`~/.opscloud/.state/`)

These files are machine-managed. Do not edit them manually.

| Path | Purpose |
|---|---|
| `sessions.db` | Conversation checkpoints, thread states, and SQLite checkpointer |
| `auth.json` | Credential store and provider metadata |
| `history.jsonl` | Interactive command input history |
| `recent_models.json` | Recent `/model` selections (up to 10 entries) |
| `mcp_trust.json` | MCP project trust decisions |
| `skill_trust.json` | Skill trust decisions |
| `onboarding_complete` | First-run onboarding marker |
| `installed_plugins.json` | Installed plugin registry |
| `plugin_state.json` | Plugin runtime state |
| `plugin_marketplaces.json` | Marketplace sources registry |

### Project-level (`.opscloud/`)

| Path | Purpose |
|---|---|
| `.opscloud/skills/` | Project custom skills |
| `.opscloud/agents/` | Project custom subagents |
| `.opscloud/plugins/` | Project custom plugins |
| `.opscloud/memory/` | Project-scoped memory entries |
| `.opscloud/AGENTS.md` | Project-level instructions (committed to Git) |
| `.opscloud/config.toml` | Project-level configuration overrides |
| `.opscloud/hooks.json` | Project-level lifecycle hooks |
| `.opscloud/.mcp.json` | Project-level MCP servers |

### Universal shared data (`~/.agents/`, `.agents/`)

| Path | Purpose |
|---|---|
| `~/.agents/skills/` | User-level tool-agnostic skills (shared across agent ecosystems) |
| `.agents/skills/` | Project-level tool-agnostic skills |

---

## Settings precedence summary

| Setting type | Resolution order (first wins) |
|---|---|
| **General options** | CLI flag → `OPSCLOUD_*` env → `config.toml` (project > user) → default |
| **Provider API keys** | `OPSCLOUD_{KEY}` env → canonical env → Project `.env` → `~/.opscloud/.env` → Cloud IAM/ADC |
| **Provider base URLs** | Stored base URL → env var → `config.toml` → default endpoint |
| **Skills** | Project `.opscloud/skills/` → User `~/.opscloud/skills/` → Plugins → Built-in |
| **Memory** | Project `.opscloud/memory/` → User `~/.opscloud/memory/` |
| **Subagents** | Project `.opscloud/agents/` → User `~/.opscloud/agents/` → Agent Plugins → Async Remote |
| **MCP servers** | `--mcp-config` → Project `.mcp.json` → Global `~/.opscloud/.mcp.json` |
| **Hooks** | Project `.opscloud/hooks.json` + Global `~/.opscloud/hooks.json` (merged) |

