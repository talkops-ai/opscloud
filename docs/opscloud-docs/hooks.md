# Hooks

> Run deterministic custom logic before or after tool execution via `hooks.json`

Hooks enable you to trigger external validation scripts, security scanners, or audit notifications whenever OpsCloud executes a tool. Engineered via `ServerHooksMiddleware`, hooks run deterministically to enforce corporate compliance, secret scanning, or auto-formatting.

---

## Configuration

Hooks are declared in JSON files at two scopes:

| Location | Scope | Trust Model |
|---|---|---|
| `~/.opscloud/hooks.json` | Global (all sessions) | Always trusted |
| `.opscloud/hooks.json` | Project-level (Git repository) | Requires `--trust-project-hooks` or interactive approval |

Both global and project-level hooks execute concurrently.

---

## Format

```json
{
  "hooks": {
    "Bash": {
      "command": "echo 'Shell command: ${input.command}' >> ~/.opscloud/audit.log"
    },
    "Write": {
      "command": "echo 'File created: ${input.file_path}' >> ~/.opscloud/audit.log"
    },
    "Edit": {
      "command": "echo 'File edited: ${input.file_path}' >> ~/.opscloud/audit.log"
    }
  }
}
```

Each key is a **tool identifier**, and `command` specifies the shell script that executes upon invocation. You can reference tool arguments using `${input.field}` interpolation.

---

## Tool mapping and input fields

OpsCloud maps internal tools to standard names for hook execution:

| Tool name | Triggered by | Available input fields |
|---|---|---|
| `Bash` | Shell commands (`execute`) | `command`, `timeout` |
| `Write` | File creation (`write_file`) | `file_path`, `content` |
| `Edit` | File modifications (`edit_file`) | `file_path`, `old_string`, `new_string`, `replace_all` |
| `Read` | File reads (`read_file`) | `file_path`, `offset`, `limit` |
| `Glob` | File path pattern searches | `pattern`, `path` |
| `Grep` | Content regex searches | `pattern`, `path`, `glob`, `output_mode` |
| `LS` | Directory listings | `path` |

MCP tools trigger using their fully qualified names:

```json
{
  "hooks": {
    "mcp__kubernetes__get_pods": {
      "command": "echo 'Kubernetes cluster inspected' >> ~/.opscloud/audit.log"
    }
  }
}
```

---

## Lifecycle events

Hooks can also bind to agent lifecycle phases:

- `pre_tool`: Executes immediately before a tool runs.
- `post_tool`: Executes immediately after a tool finishes.
- `pre_subagent`: Executes prior to delegating to a domain subagent.
- `post_subagent`: Executes when a subagent completes and yields control back.

### Injected environment variables

Hook commands receive execution context via environment variables:

| Variable | Description |
|---|---|
| `OPSCLOUD_TOOL_NAME` | The active tool being invoked (e.g., `execute`, `write_file`) |
| `OPSCLOUD_TOOL_INPUT` | Complete JSON payload of the tool's input arguments |
| `OPSCLOUD_TOOL_OUTPUT` | Complete JSON payload of the tool's result (in `post_tool` hooks) |
| `OPSCLOUD_SESSION_ID` | Active session thread identifier |

---

## Exit code contracts

- **Exit code `0`**: Success / Allow. Tool execution proceeds normally.
- **Non-zero exit code**: Abort. OpsCloud immediately cancels the tool call, captures the hook's `stderr`, and feeds the error message back to the agent so it can self-correct.

---

## Practical examples

### 1. Pre-commit secret scanning
Halt file writes if credentials or secrets are detected in the payload:

```json
{
  "hooks": {
    "Write": {
      "command": "python3 -c \"import sys, re; content = open('${input.file_path}').read(); sys.exit(1) if re.search(r'(AKIA[0-9A-Z]{16}|ghp_[0-9a-zA-Z]{36})', content) else sys.exit(0)\""
    }
  }
}
```

### 2. Auto-format on file save
Automatically format Terraform or OpenTofu manifests whenever the agent writes or edits them:

```json
{
  "hooks": {
    "Write": {
      "command": "if echo '${input.file_path}' | grep -qE '\\.(tf|tofu)$'; then tofu fmt '${input.file_path}' 2>/dev/null || terraform fmt '${input.file_path}' 2>/dev/null || true; fi"
    }
  }
}
```

### 3. Slack notifications for infrastructure plans
Send webhook alerts whenever the agent runs plan or dry-run commands:

```json
{
  "hooks": {
    "Bash": {
      "command": "if echo '${input.command}' | grep -qE '(terraform|tofu) plan'; then curl -s -X POST -H 'Content-type: application/json' --data '{\"text\":\"OpsCloud initiated infrastructure plan\"}' $SLACK_WEBHOOK_URL; fi"
    }
  }
}
```

---

## Trust and security

Project-level hooks (`.opscloud/hooks.json`) can execute arbitrary shell commands when repositories are cloned. To protect your machine, project hooks require explicit trust:

```bash
opscloud --trust-project-hooks
```

Global hooks (`~/.opscloud/hooks.json`) are always trusted.
