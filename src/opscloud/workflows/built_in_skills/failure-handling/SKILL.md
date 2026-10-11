---
name: failure-handling
description: "What to do when a workflow step errors, times out, or produces an unexpected result. Read as soon as any step fails. Covers classifying the failure (transient, input, permission, precondition, real defect), bounded retries, following the definition's failure path, partial-completion state, and choosing between blocked and failed."
license: MIT
compatibility: designed for opscloud
metadata:
  workflow: "false"
---

# Handling failures

A failed step is information, not an emergency. Diagnose briefly, respond
proportionally, and always leave the operator knowing exactly where things stand.

## 1. Read the error before reacting

Look at the full error output: the exit code, the message, and the last relevant lines.
Then classify it:

| Class | Signals | Response |
|---|---|---|
| **Transient** | timeouts, 429/503, "connection reset", lock contention, eventual-consistency "not found" right after a create | Retry with bounded backoff (§2) |
| **Input** | "not found", invalid value, wrong format, wrong resource name | Re-check the resolved inputs. If an input is wrong, ask (the run is `blocked`) |
| **Permission / auth** | 401/403, AccessDenied, expired token, MFA required | `blocked`. Never escalate privileges or switch identities |
| **Precondition** | resource busy, dependency unhealthy, quota exceeded, change freeze, dirty state | Follow the definition's remedy if it has one, otherwise `blocked` |
| **Tool / environment** | CLI missing, wrong version, plugin MCP server down | `blocked` with the exact missing piece |
| **Real failure** | the operation ran and the system rejected it, or the outcome is wrong (deployment unhealthy, tests fail, migration error) | Definition's failure path (§3), otherwise `failed` |
| **Approval rejected** | the operator rejected a tool call | Not an error to work around. Ask how to proceed |

If you can't tell which class it is, gather **one or two** read-only facts (status,
events, logs, describe) before deciding.

## 2. Retries

- Retry **only transient failures**, and only for steps that are safe to repeat
  (idempotent, or verified not to have applied).
- Use the definition's policy. Without one, retry at most **3 times** with increasing
  delay (for example 10s, 30s, 60s).
- Before retrying a mutating step, check whether the first attempt actually applied.
  Many "timeouts" succeed on the server.
- Do not retry with different flags, a force option or another tool to get past an
  error. That is a different action, and it needs the operator.

## 3. Follow the definition's failure path

Definitions often say what to do on failure: "on failure, roll back", "goto step N",
"notify #channel and stop", "continue with the remaining services". When the failure
matches:

- Take that path and say so: `Step 5 failed (rollout timeout) → definition says roll back (step 9)`.
- The failure path is itself a set of steps. Execute and verify it with the same care.

If the definition has no failure path for this case, do not invent remediation.
Investigate read-only, then ask or finish as `blocked`/`failed`.

## 4. Partial completion

Before you stop, make the current state explicit:

- **Done**: completed steps, with evidence.
- **Not started**: remaining steps.
- **In-between**: anything half-applied, such as 3 of 8 services updated, a rollout
  paused, a lock held, or a temp resource left behind.
- **Before-state**: where it was captured, and how to restore it.

Release anything the run holds, such as locks, port-forwards or temporary access,
unless releasing it would make things worse. Say which.

## 5. blocked vs failed

- **`blocked`**: the run *can* continue once the operator acts. They might provide an
  input, fix a credential, approve something, decide a branch, or wait for a freeze to
  end. Call `update_workflow(status="blocked")`, then ask one clear question. The
  operator's reply resumes the same run.
- **`failed`**: the run cannot reach its goal as defined. A step failed for real and
  there is no recovery path, or the recovery path itself failed. Call
  `update_workflow(status="failed")` with the failing step, the error, and the state
  left behind.

When in doubt, prefer `blocked` with a precise question. That keeps the operator in
control of what happens next.

## 6. What not to do

- Don't claim success when a step only "mostly" worked.
- Don't suppress errors (`|| true`, `--ignore-errors`, `2>/dev/null`) to keep going.
- Don't delete and recreate things to get past an error unless the definition says so.
- Don't loop: if the same failure happens twice after a fix attempt, stop and report.
