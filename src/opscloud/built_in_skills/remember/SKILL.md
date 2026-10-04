---
name: remember
description: "Distill the conversation to capture cloud infrastructure decisions, DevOps patterns, operator preferences, and operational runbooks into persistent memory (AGENTS.md) or reusable OpsCloud skills. Use when the operator says: (1) remember this, (2) save cloud preferences, (3) update memory, (4) persist operational learnings."
license: MIT
compatibility: designed for opscloud
---

Review the conversation and capture valuable operational knowledge. Focus especially on **cloud & DevOps best practices**, **infrastructure conventions**, and **operator preferences**—these are the most important things to preserve across sessions.

## Step 1: Identify Best Practices and Key Learnings

Scan the conversation for:

### Cloud Infrastructure & DevOps Practices (highest priority)
- **Patterns that worked well** - cloud architectures, Terraform modules, Helm values, Kubernetes configurations, and deployment pipelines proven effective
- **Gotchas and anti-patterns to avoid** - cloud misconfigurations, IAM privilege pitfalls, security group leaks, egress cost surprises, or brittle CLI commands
- **Operational & reliability standards** - SLAs, backup policies, tagging conventions, health check setups, and disaster recovery procedures
- **Architectural decision rationale** - why specific instance types (e.g. Graviton), regions, cloud providers, or networking topologies were selected

### Operator & Environment Context
- Operator personal details (names, family context, preferences) and communication style
- Active cloud profiles (e.g., AWS profiles like `krayak`), default accounts, and preferred regions
- Project infrastructure layout, workspace paths, and toolchain versions
- Operator feedback on agent behavior, automated remediation steps, or approval thresholds

## Step 2: Decide Where to Store Each Learning

For each learning or convention, choose the appropriate destination:

### -> Persistent Memory (AGENTS.md) for preferences, conventions, and rules
Use persistent memory when the knowledge is:
- An operator preference, personal detail, or environment default
- A global or project cloud convention (e.g., "always deploy to us-west-2", "tag all AWS resources with CostCenter")
- A rule or constraint the agent must always respect

**Global** (`$OPSCLOUD_HOME/AGENTS.md` or `~/.opscloud/AGENTS.md`): Universal operator identity, personal details, cross-cloud defaults, and global DevOps preferences
**Project** (`.opscloud/AGENTS.md` or `AGENTS.md`): Project-specific infrastructure conventions, cloud accounts, Terraform workspace rules, and cluster topologies

### -> Reusable Skill for multi-step DevOps workflows and runbooks
**Create a skill when** we developed:
- A multi-step operational runbook or deployment workflow
- A methodology for troubleshooting complex cloud incidents (e.g., EKS node drain failures, VPC peering issues)
- An infrastructure automation pattern with safety checks and best practices baked in
- A procedure that should be executed consistently

Skills are more powerful than memory entries because they can encode **how** to execute operational procedures safely, not just **what** to remember.

## Step 3: Create Skills for Significant Operational Procedures

If we established best practices around a cloud workflow or runbook, capture them in a reusable skill.

**Example:** If we established a standard procedure for provisioning EKS clusters with Karpenter, rotating IAM credentials, or triaging S3 lifecycle rules, create an OpsCloud skill that encodes those practices into a structured workflow.

### Skill Location
- Global: `$OPSCLOUD_HOME/skills/<skill-name>/SKILL.md` (or `~/.opscloud/skills/<skill-name>/SKILL.md`)
- Project: `.opscloud/skills/<skill-name>/SKILL.md`

### Skill Structure
```
skill-name/
├── SKILL.md          (required - main instructions with best practices)
├── scripts/          (optional - automation scripts)
├── references/       (optional - cloud specs, architecture docs)
└── assets/           (optional - templates, policy examples)
```

### SKILL.md Format
```markdown
---
name: skill-name
description: "What this cloud skill does AND when to activate it. Include specific triggers like 'when deploying to EKS' or 'when configuring AWS VPC'."
---

# Skill Name

## Overview
Brief explanation of what this cloud skill accomplishes.

## Best Practices
Capture the key cloud & DevOps practices upfront:
- Best practice 1: explanation
- Best practice 2: explanation

## Operational Procedure
Step-by-step instructions (imperative form):
1. First, verify active cloud credentials and target account
2. Next, validate configuration with dry-run or plan
3. Apply changes and verify health metrics

## Common Pitfalls
- Pitfall to avoid and why
- Anti-pattern discovered during past operations
```

### Key Principles
1. **Encode safety & best practices prominently** - Put credential checks and dry-run steps near the top
2. **Concise is key** - Only include non-obvious knowledge; justify every paragraph's token cost
3. **Clear triggers** - The description determines when OpsCloud activates the skill; be specific
4. **Imperative form** - Write commands: "Verify cluster status" not "You should verify cluster status"
5. **Include anti-patterns** - What NOT to do in production is often as critical as what to do

## Step 4: Update Persistent Memory for Simpler Learnings

For operator preferences, identity details, and DevOps guidelines that don't warrant a full skill:

```markdown
## Operator Preferences & Identity
- Preferred name: Sandeep
- Personal / family context: ...
- Preferred AWS region: us-west-2
- Always use Graviton (arm64) instances for cost efficiency

## Infrastructure Standards & Conventions
- Enforce tags: Environment, Owner, ManagedBy=OpsCloud
- Avoid public S3 buckets unless explicitly authorized
```

Use `read_file` to inspect the target `AGENTS.md` (e.g. `~/.opscloud/AGENTS.md`) first. Check if the section or preference already exists. Then use `edit_file` to update/insert, or `write_file` if creating anew. Preserve existing markers and structure.

## Step 5: Summarize Changes

List what was captured and where it was stored:
- Skills created (with key operational practices encoded)
- Memory entries added (with location e.g. `Saved to persistent memory (~/.opscloud/AGENTS.md):`)
