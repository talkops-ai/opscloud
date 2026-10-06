# Remote sandboxes

> Run OpsCloud in isolated cloud environments instead of on your local machine

Remote sandboxes allow OpsCloud to execute tools (shell commands, file operations, heavy compilation, and test suites) within an isolated, ephemeral cloud container instead of directly on your local workstation.

This provides:
- **Blast radius elimination** — Safely run mutating infrastructure commands without risking local files or configuration.
- **Zero local footprint** — Build large container images or compile heavy codebases without consuming workstation CPU or RAM.
- **Credential safety** — Prevent access to local SSH private keys, personal files, or shell history.
- **CI/CD reproducibility** — Guarantee clean, consistent container environments for pull request validation.

---

## Launch a sandbox

```bash
# Launch with the default sandbox provider
opscloud --sandbox

# Launch with a specific provider
opscloud --sandbox daytona

# Attach to an existing running sandbox container
opscloud --sandbox-id sb-prod-cluster-98234

# Launch from a pre-baked snapshot with a setup script
opscloud --sandbox modal \
  --sandbox-snapshot-name terraform-k8s-base \
  --sandbox-setup ./scripts/sandbox-init.sh
```

### CLI flags

| Flag | Description |
|---|---|
| `--sandbox [TYPE]` | Enable a sandbox provider (`agentcore`, `daytona`, `langsmith`, `modal`, `runloop`, `vercel`; default: `none`) |
| `--sandbox-id ID` | Attach to an already running sandbox instance |
| `--sandbox-snapshot-name NAME` | Launch from a pre-baked container blueprint or snapshot |
| `--sandbox-setup PATH` | Path to a shell script executed inside the sandbox immediately after startup |

---

## Supported providers

| Provider | Identifier | Default Workdir | Highlights |
|---|---|---|---|
| **AgentCore** | `agentcore` | `/tmp` | Managed AWS execution environment |
| **Daytona** | `daytona` | `/home/daytona` | Self-hosted and cloud development environments |
| **LangSmith** | `langsmith` | `/root` | Ephemeral evaluation sandboxes |
| **Modal** | `modal` | `/workspace` | Serverless high-performance GPU/CPU containers |
| **Runloop** | `runloop` | `/home/user` | Fast container boot with snapshot caching |
| **Vercel** | `vercel` | `/vercel/sandbox` | Ephemeral code sandboxes |
| **Docker** | `docker` | `/workspace` | Local daemon container isolation |

---

## Workspace sync semantics

When a sandbox session starts, OpsCloud automatically synchronizes your local project workspace to the remote container:
1. Detects project boundaries and Git status.
2. Traverses the repository tree, automatically skipping ignored patterns (`.git/`, `node_modules/`, `.venv/`, `__pycache__/`, `.opscloud/.state/`).
3. Executes tools, builds, and tests inside the remote container.
4. Synchronizes modified files and generated artifacts back to your local workstation upon completion.

---

## Setup scripts

Execute initialization tasks inside the sandbox immediately upon boot:

```bash
opscloud --sandbox modal --sandbox-setup ./scripts/ci-setup.sh
```

**`./scripts/ci-setup.sh`:**
```bash
#!/usr/bin/env bash
set -euo pipefail

# Install required DevOps toolchains
apt-get update && apt-get install -y opentofu kubectl helm awscli

# Verify toolchain availability
tofu version
kubectl version --client=true
helm version
```

---

## Intelligent tool routing

When a remote sandbox is active, OpsCloud routes tool calls intelligently:

- **Shell commands, file reads/writes, grep, glob** ──> Routed to the **Remote Sandbox**
- **Web search, URL fetching** ──> Handled on the **Local Host** (eliminating remote proxy overhead)

Sandboxes terminate and clean up automatically when your session exits.

---

## CI/CD zero-trust pipeline example

Combine sandboxes with headless mode and rubric grading for zero-trust automated pull request validation:

```bash
opscloud -n "Run OpenTofu plan and check for security violations" \
  --sandbox runloop \
  --sandbox-setup ./scripts/ci-init.sh \
  --rubric "tofu plan succeeds with zero syntax errors and no open security group ingress" \
  --quiet \
  -y
```
