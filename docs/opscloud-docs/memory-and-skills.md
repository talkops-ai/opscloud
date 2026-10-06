# Memory and skills

> Persistent memory across sessions and reusable skills for domain expertise

OpsCloud provides two complementary mechanisms to adapt to your workflows:

* **Memory**: `AGENTS.md` files and SQLite checkpoints that persist across sessions. Use memory for coding conventions, team standards, and project architecture.
* **Skills**: Reusable, on-demand instructions that OpsCloud discovers and loads dynamically. Use skills for domain-specific workflows, cloud APIs, and reference patterns.

Use `/remember` inside any session to prompt OpsCloud to extract learnings and persist them to memory or new skills.

---

## Memory

### Automatic convention learning

As you collaborate with OpsCloud, it can record operational patterns for future sessions:

```text
> We use AWS KMS customer-managed keys for all S3 buckets and enforce bucket-owner-enforced object ownership
```

Tell OpsCloud to remember:

```text
/remember Our S3 buckets always require KMS CMK encryption and bucket-owner-enforced ownership
```

OpsCloud records the convention to persistent storage and applies it to subsequent sessions.

### Where memories are stored

| Scope | Path | When it loads |
|---|---|---|
| **Project** (higher priority) | `.opscloud/memory/` | Whenever running inside that repository |
| **User** (fallback) | `~/.opscloud/memory/` | Every session across all directories |

When both scopes contain a memory with the same key, the project version takes precedence.

### Memory limits

To prevent memory entries from exhausting model context windows:
- Maximum **200 lines** injected per session
- Maximum **25 KB** total memory payload per session

### Managing memory

Use the `/memory` slash command in the TUI:

```text
/memory                    # List all active memories
/memory save <key>         # Save a new memory entry
/memory delete <key>       # Remove a stored memory entry
```

### `AGENTS.md` files

`AGENTS.md` files define persistent instructions that load automatically at session start:

| Path | Scope | Purpose |
|---|---|---|
| `.opscloud/AGENTS.md` | Project-level | Shared team conventions (committed to Git) |
| `AGENTS.md` | Repository root | Alternative project-level location |
| `~/.opscloud/{agent}/AGENTS.md` | User-level | Personal developer preferences |

---

## Skills

Skills are modular instruction sets that endow OpsCloud with deep domain knowledge. Each skill lives in a directory containing a required `SKILL.md` file.

### Two kinds of skills

- **Global skills**: Loaded into the root orchestrator and accessible across tasks (`cloud-core`, `docker`, `kubernetes`, `remember`).
- **Subagent skills**: Bound to specific subagents (Terraform, Helm, Ansible, OpenTofu). They only consume tokens when that subagent is actively invoked.

### Skill directory structure

```
skill-name/
├── SKILL.md          # Required — instructions with YAML frontmatter
├── scripts/          # Optional — helper scripts executed by the agent
├── references/       # Optional — domain documentation and cheat sheets
└── assets/           # Optional — templates, examples, and schemas
```

### `SKILL.md` format

```markdown
---
name: kubernetes
description: "Author, audit, and troubleshoot Kubernetes manifests following Pod Security Standards"
---

# Kubernetes Engineering Skill

When authoring or debugging manifests:
1. Always define explicit CPU and memory requests and limits.
2. Configure readinessProbe and livenessProbe on all long-running workloads.
3. Enforce non-root securityContext (runAsNonRoot: true).
4. Include standard labels: app.kubernetes.io/name, instance, version.
```

### 7-tier skill resolution hierarchy

OpsCloud discovers skills across multiple locations. If two skills share the same name, higher priority sources override lower ones:

| Priority | Source | Location |
|---|---|---|
| **1 (Highest)** | Project Agents Skills | `.agents/skills/` |
| **2** | Project OpsCloud Skills | `.opscloud/skills/` |
| **3** | User Agents Skills | `~/.agents/skills/` |
| **4** | User OpsCloud Skills | `~/.opscloud/skills/` |
| **5** | Marketplace Plugin Skills | Installed plugins directory |
| **6** | Subagent Skills | Built-in subagent packages |
| **7 (Lowest)** | Global Built-in Skills | Ships with OpsCloud (`cloud-core`, `docker`, etc.) |

### Progressive disclosure

To preserve context window capacity, OpsCloud does not inject full skill text at session startup. Instead, it registers lightweight metadata (name and description). When the model determines that a skill is required, it triggers an on-demand retrieval tool to load the complete `SKILL.md` instructions.

---

## Using skills

### Automatic activation
Skills activate automatically based on their description when OpsCloud recognizes a relevant prompt or workspace file.

### Manual invocation
```text
/skills                    # Browse all loaded skills
/skill kubernetes          # Explicitly invoke a skill
```

### Pre-load at launch
```bash
opscloud -s kubernetes     # Pre-load a skill at startup
```

### Create custom skills
Scaffold a new skill directory:

```bash
opscloud skills create my-custom-skill
```

Or author `SKILL.md` directly in `.opscloud/skills/my-custom-skill/`.

### Distill skills from conversation
Use `/skill-create` inside an interactive session. OpsCloud analyzes the conversation history, extracts proven workflows, and authors a structured `SKILL.md` file for future re-use.

### Skill trust verification
To protect developers from untrusted scripts in cloned repositories, OpsCloud tracks trust decisions. You will be prompted to approve a skill the first time it is discovered in a new project.
