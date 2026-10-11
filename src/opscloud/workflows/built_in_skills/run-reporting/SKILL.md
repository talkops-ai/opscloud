---
name: run-reporting
description: "How to communicate during a workflow and how to write the final outcome: concise progress updates, evidence-backed step results, the update_workflow note and artifacts, and the final operator summary for complete, blocked, and failed runs. Read before calling update_workflow, and whenever the definition asks for a report, a notification, or a deliverable."
license: MIT
compatibility: designed for opscloud
metadata:
  workflow: "false"
---

# Reporting a workflow run

The operator will often read the end of the run without watching the middle.
The report must stand on its own: what ran, what changed, what proves it, and
what (if anything) they need to do.

## 1. During the run

- **One short line per step transition** is enough, for example
  `Step 3/7 ✓ image updated to v2.4.1 (rollout 6/6 ready)`. Don't narrate tool calls.
- Announce branch decisions with their evidence (see workflow-execution §4).
- Before a mutating step, say in one line what is about to change and on which target.
  That gives the approval prompt context.
- Put bulky output (logs, plans, query results) in files, not in chat.

## 2. Evidence

Each completed step should be backed by something observable:

| Step type | Good evidence |
|---|---|
| Deploy / rollout | new revision or tag, ready count, health check result |
| Config / infra change | before → after values, plan/apply summary (`2 changed, 0 destroyed`) |
| Data operation | rows affected, checksum or count before and after |
| Ticket / PR / notification | URL or ID |
| Diagnosis | the specific log line, metric value or event that supports the conclusion |
| Generated deliverable | absolute file path, plus a one-line description |

"Command exited 0" is weak evidence. Prefer a state read after the change.

## 3. `update_workflow` note

Write the `note` for someone who reads only this field. Keep it to roughly 3–10 lines:

- **complete**: outcome in one sentence; key changes as before → after; verification
  evidence; follow-ups, if any.
- **blocked**: the step you stopped at; what exactly is needed from the operator;
  what has already been done; anything currently half-applied.
- **failed**: the failing step and the error (the key line, not a dump); the
  recovery attempted, if any; the exact state left behind, and how to restore it.

`artifacts`: absolute paths (or URLs) of files and resources the run produced and
the operator may need, such as reports, plans, logs, PRs or tickets. Leave out
scratch files.

Never put secrets or full sensitive payloads in the note or artifacts.

## 4. Final message to the operator

After `update_workflow`, reply in this shape. Keep it scannable:

```
✅ <workflow> complete — <one-line outcome>

Inputs: env=prod, service=checkout, version=v2.4.1
Changes:
  • checkout image v2.4.0 → v2.4.1 (payments ns)
  • HPA min replicas 2 → 3
Verification: rollout 6/6 ready, error rate 0.3% (baseline 0.4%)
Artifacts: /abs/path/report.md, https://github.com/org/repo/pull/123
Follow-ups: none
```

For `blocked`, lead with the question. For `failed`, lead with the current state of the
system before the details. Use ⏸ or ❌ instead of ✅.

## 5. Deliverables the definition asks for

When the workflow's purpose is to produce something (a report, a postmortem draft, a
change summary, an audit result, a generated config):

- Use the template or format from the definition, or a template file next to SKILL.md
  when there is one.
- Write the deliverable to a file, and list it in artifacts.
- In chat, give only a short summary of it.
- Mark facts you verified separately from assumptions or inferences.

If the definition asks you to post somewhere (Slack, a ticket, an email), that is a
mutating step. It goes through approval like any other, and the message's ID or URL is
the evidence.
