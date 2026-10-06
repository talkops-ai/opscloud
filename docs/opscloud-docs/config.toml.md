# config.toml Reference

> Complete reference for `~/.opscloud/config.toml` — models, Jev router pools, UI settings, permissions, and compaction

OpsCloud reads its main configuration from `~/.opscloud/config.toml` (user-global) and `.opscloud/config.toml` (project-level). Use `/config` inside an interactive session to view and modify settings, or `opscloud config show` from the shell. See [Configuration](./Configuration.md) for how settings resolve across environment variables, `config.toml`, and defaults.

## File locations

```
~/.opscloud/config.toml   # User-level settings
.opscloud/config.toml    # Project-level overrides
```

---

## Startup and general models

```toml
[startup]
mode = "smart"                            # manual, auto, or smart
agent = "general"                         # default subagent to launch

[model]
default = "anthropic:claude-3-7-sonnet-20250219"   # Default model specifier
reasoning_effort = "medium"                        # off, low, medium, or high
```

| Key | Section | Type | Default | Description |
|---|---|---|---|---|
| `mode` | `startup` | choice | `"smart"` | Default approval mode: `manual`, `auto`, `smart` |
| `agent` | `startup` | string | `"general"` | Default subagent to launch |
| `default` | `model` | string | *(none)* | Default model specifier in `provider:model-name` format |
| `reasoning_effort` | `model` | choice | `"medium"` | Unified reasoning effort level: `off`, `low`, `medium`, `high` |

Set the default model from the CLI:

```bash
opscloud --default-model anthropic:claude-3-7-sonnet-20250219
```

---

## Jev dynamic model router pool

OpsCloud includes the **Jev TypeSafe Dynamic Model Router**, which classifies prompts in `<70ms` and assigns them to the appropriate tier:

```toml
[agent_pool]
provider = "anthropic"
fast = "anthropic:claude-3-5-haiku-latest"
standard = "anthropic:claude-3-5-sonnet-latest"
powerful = "anthropic:claude-3-7-sonnet-20250219"
fast_effort = "off"
standard_effort = "low"
powerful_effort = "high"
```

| Key | Type | Default | Description |
|---|---|---|---|
| `provider` | string | `"anthropic"` | Primary pool provider family |
| `fast` | string | Claude Haiku / GPT-4o-mini | Model for quick searches, file reads, and lint runs |
| `standard` | string | Claude Sonnet / GPT-4o | Model for standard coding and IaC generation |
| `powerful` | string | Claude 3.7 / o1 / DeepSeek R1 | Model for complex architecture, subtle bugs, and root-cause analysis |
| `fast_effort` | choice | `"off"` | Reasoning effort for the fast tier |
| `standard_effort` | choice | `"low"` | Reasoning effort for the standard tier |
| `powerful_effort` | choice | `"high"` | Reasoning effort for the powerful tier |

---

## Providers

Configure provider-specific settings, custom base URLs, and private endpoints:

```toml
[providers.openai]
enabled = true
base_url = "https://api.openai.com/v1"
models = ["gpt-4o", "gpt-4o-mini", "o1", "o3-mini"]

[providers.anthropic]
enabled = true
models = ["claude-3-5-sonnet-latest", "claude-3-7-sonnet-20250219", "claude-3-5-haiku-latest"]

[providers.ollama]
enabled = true
base_url = "http://localhost:11434"
models = ["llama3.3", "qwen2.5-coder:32b"]
```

| Key | Type | Description |
|---|---|---|
| `enabled` | bool | Whether this provider is active |
| `models` | list[string] | Model identifiers available from this provider |
| `base_url` | string | Override the default API endpoint (for private proxies or VPC endpoints) |
| `base_url_env` | string | Environment variable name for the base URL override |
| `api_key_env` | string | Environment variable name for the API key |
| `params` | table | Extra kwargs passed directly to the model constructor |
| `display_name` | string | Human-readable provider name |

---

## Display and UI

```toml
[ui]
theme = "tokyo-night"                     # TUI color theme
show_scrollbar = false                    # Vertical scrollbar in chat area
show_timestamps = true                    # Display timestamps on messages
auto_scroll = true                        # Auto-scroll on incoming stream tokens
notifications = true                      # Desktop notification alerts
show_turn_duration = true                 # Display agent turn timer
subagent_panel_open = true                # Keep live subagent telemetry panel visible
stream_tokens = true                      # Stream tokens in real-time
verbose = false                           # Verbose debug output
```

| Key | Type | Default | Description |
|---|---|---|---|
| `theme` | string | `"tokyo-night"` | Active TUI color theme |
| `show_scrollbar` | bool | `false` | Show vertical scrollbar in the chat pane |
| `show_timestamps` | bool | `true` | Show timestamps on chat messages |
| `auto_scroll` | bool | `true` | Automatically scroll to newest tokens |
| `notifications` | bool | `true` | Enable desktop notification toasts on task completion |
| `show_turn_duration` | bool | `true` | Show execution duration timer on each turn |
| `subagent_panel_open`| bool | `true` | Show live `SubagentPanel` telemetry widget |
| `stream_tokens` | bool | `true` | Enable real-time token streaming |
| `verbose` | bool | `false` | Enable verbose debugging output |

---

## Tools and shell allowlists

```toml
[tools]
shell_allow_list = ["terraform", "tofu", "kubectl", "helm", "ansible-playbook"]
timeout_seconds = 120
max_output_bytes = 1048576
```

| Key | Type | Default | Description |
|---|---|---|---|
| `shell_allow_list` | list[string] | `["recommended"]` | Shell commands allowed without interactive approval |
| `timeout_seconds` | int | `120` | Tool execution timeout limit |
| `max_output_bytes` | int | `1048576` | Maximum stdout/stderr output size captured before truncation |

### Shell allowlist modes

- `"recommended"` — Curated set of safe, read-only commands (`ls`, `cat`, `grep`, `kubectl get`, `terraform validate`, etc.)
- `"all"` — Allow all shell commands without prompt (use with caution)
- List of strings — Explicit binaries (e.g., `["tofu", "terraform", "kubectl", "helm"]`)

---

## Permissions

Fine-grained controls over tool categories:

```toml
[permissions]
shell_read = true                         # Allow non-mutating shell commands
shell_write = false                       # Allow mutating shell commands
file_read = true                          # Allow file reads (read_file, grep, glob, ls)
file_write = false                        # Allow file writes, edits, and deletions
infra_plan = false                        # Allow infrastructure dry-runs (terraform plan)
infra_apply = false                       # Allow infrastructure mutations (terraform apply)
auto_approved_tools = ["read_file", "ls", "glob", "grep"]
forbidden_paths = ["/etc", "/var", "/root", "~/.ssh"]
```

---

## Context compaction

Controls automated background conversation summarization for long-running sessions:

```toml
[compaction]
token_threshold = 30000                   # Context window token count triggering compaction
summarization_model = "anthropic:claude-3-5-haiku-latest"
offload_directory = "memory/archive"       # Where deep conversation trails are written
```

| Key | Type | Default | Description |
|---|---|---|---|
| `token_threshold` | int | `30000` | Token limit triggering background summarization |
| `summarization_model` | string | Fast tier model | Model used to generate compact summaries |
| `offload_directory` | string | `"memory/archive"` | Path to offloaded session history markdown files |

---

## Complete configuration example

```toml
[startup]
mode = "smart"
agent = "general"

[model]
default = "anthropic:claude-3-7-sonnet-20250219"
reasoning_effort = "medium"

[agent_pool]
provider = "anthropic"
fast = "anthropic:claude-3-5-haiku-latest"
standard = "anthropic:claude-3-5-sonnet-latest"
powerful = "anthropic:claude-3-7-sonnet-20250219"
fast_effort = "off"
standard_effort = "low"
powerful_effort = "high"

[providers.anthropic]
enabled = true

[providers.openai]
enabled = true

[providers.ollama]
enabled = true
base_url = "http://localhost:11434"
models = ["llama3.3", "qwen2.5-coder:32b"]

[ui]
theme = "tokyo-night"
show_timestamps = true
subagent_panel_open = true
notifications = true

[tools]
shell_allow_list = ["tofu", "terraform", "kubectl", "helm"]
timeout_seconds = 180

[permissions]
shell_read = true
shell_write = false
file_read = true
file_write = false
infra_plan = false
infra_apply = false

[compaction]
token_threshold = 30000
offload_directory = "memory/archive"
```
