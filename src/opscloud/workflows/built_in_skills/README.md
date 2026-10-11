# Workflow agent built-in skills

Skills in this directory are attached **only** to the isolated `/workflow`
workflow agent — never to the main deep agent or subagents. They carry
domain-neutral know-how for running *any* skill-defined workflow; the workflow
definition selected by the operator is injected separately and in full, and
always takes precedence over this general guidance.

| Skill | Read when |
|---|---|
| [`workflow-inputs`](workflow-inputs/SKILL.md) | Start of a run — parse, default, derive, validate, ask once, echo inputs |
| [`workflow-execution`](workflow-execution/SKILL.md) | Before the first step — preflight, todos, check-before-change, branches, bounded waits, approvals |
| [`change-safety`](change-safety/SKILL.md) | Before the first mutating step — risk classes, sensitive targets, before-state, stop conditions, rollback |
| [`failure-handling`](failure-handling/SKILL.md) | When a step fails — classify, bounded retries, failure paths, partial state, blocked vs failed |
| [`run-reporting`](run-reporting/SKILL.md) | Before `update_workflow` — progress lines, evidence, note/artifacts, final summary |

The workflow agent's system prompt (`opscloud/workflows/prompts.py`) tells the
agent when to read each one. Keep that list in sync when adding or renaming
skills here.

## Adding a skill

One folder per skill with a `SKILL.md` using standard frontmatter. Mark it
`workflow: "false"` so it is never offered as a triggerable workflow:

```yaml
---
name: my-guidance
description: "What it covers and exactly when the workflow agent should read it."
metadata:
  workflow: "false"
---
```

Keep guidance domain-neutral (it applies to DevOps, SRE, platform, data, and
any other workflow). Domain-specific procedures belong in the workflow skills
themselves (plugins or local skills), not here.
