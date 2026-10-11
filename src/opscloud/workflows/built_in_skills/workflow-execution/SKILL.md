---
name: workflow-execution
description: "Step-by-step execution discipline for any workflow: preflight checks, translating the definition into todos, check-before-change, evaluating conditions and branches against evidence, bounded polling, waits and loops, long-running commands, and what to do when the definition does not cover a situation. Read after inputs are resolved and before running the first step."
license: MIT
compatibility: designed for opscloud
metadata:
  workflow: "false"
---

# Executing a workflow

A workflow definition is a contract. Your job is to carry it out faithfully,
observably and predictably, the way a well-written pipeline would, while using
judgement where a pipeline would just crash.

## 1. Turn the definition into a plan you can track

- Seed `write_todos` with **one item per step in the definition**, in the definition's
  order, using its wording. Add branch steps when you reach them, not ahead of time.
- Keep exactly one item `in_progress`. Mark an item `completed` only once you have
  evidence that it worked. A command that merely ran is not enough.
- Number steps as the definition does. If the definition has none, number them yourself
  so updates and reports can say "step 4".

## 2. Preflight before the first mutating step

Spend a few read-only calls confirming that the run can succeed:

- **Tooling**: the CLIs and MCP tools the steps need are available (`which`, `--version`,
  a cheap MCP read).
- **Identity and target**: you are pointed at the right place. Check the cloud
  account/project/subscription, the kube context, the git remote and branch, the
  API base URL. **A mismatch between the resolved inputs and the active context is a
  hard stop**: report it and ask.
- **Preconditions** the definition states: clean working tree, healthy baseline,
  change window, an approved ticket, and so on.
- **Current state**: capture the "before" state of whatever will change, such as
  replicas, image tag, config values, DNS records or file checksums. Rollback and the
  final report both need it.

If preflight fails and the definition gives no remedy, the run is `blocked`.

## 3. Check before you change

- **Idempotency first**: if the desired state is already in place, record that the step
  is "already satisfied" with evidence, and move on. Do not re-apply changes for nothing.
- **Dry-run when available**: use `plan`, `--dry-run`, `diff`, `--check` or `what-if`
  before the real change, unless the definition says otherwise. Read the dry-run
  output. If it touches more than the step intends, stop and ask.
- **Scope tightly**: always pass explicit targets (namespace, resource name, `-target`,
  label selector, `--region`). Never rely on ambient defaults for a mutating command.

## 4. Conditions and branches

Definitions express logic in many styles, for example "if X then Y", decision tables,
"on failure goto step 7", "repeat until healthy", or "skip for non-prod". For each one:

1. Decide what observation answers the condition, and get it with a tool call.
2. Compare it literally with the condition. Thresholds are exact: "above 5%" means
   5.0% does **not** match.
3. In one line, state the branch you took and the evidence for it, for example
   `Error rate 7.2% > 5% → taking the rollback branch (step 6)`.
4. If the observation is ambiguous, missing, or matches no branch, do **not** pick one.
   Gather one more piece of evidence, then ask the operator.

Never decide a branch from expectations, earlier runs, or what "usually" happens.

## 5. Waits, polling and loops

Anything that waits, such as rollouts, jobs, health checks, propagation or approvals in
external systems, must be **bounded**:

- Use the definition's timeout and interval. If it gives none, default to
  **10 minutes total, polling every 15–30 seconds**, and say so.
- Prefer native waiters (`kubectl rollout status --timeout`, `aws ... wait`,
  `gh run watch`) over hand-rolled sleep loops.
- Loops ("for each service", "retry until") need a known item list or a maximum
  iteration count *before* they start. Report progress as `3/8 done`.
- When a wait times out, treat it as a failed observation, not success. Follow the
  definition's failure path.

## 6. Long-running and noisy commands

- Redirect large outputs to a file and summarise the relevant lines in the conversation.
  Keep the file path for artifacts.
- Run independent read-only checks in parallel when the definition allows it. Keep
  mutating steps sequential unless the definition explicitly allows parallelism.
- Do not start background processes that outlive the run, unless the definition asks
  for them. If you do, record how to stop them.

## 7. Tool approvals

Mutating tools, including plugin MCP tools, go through the operator's approval policy.

- Before a gated call, make sure the call itself is self-explanatory: precise arguments
  and an explicit target.
- If a call is **rejected**, do not retry it with different flags, a different tool,
  or a shell equivalent. Ask how to proceed, or mark the run `blocked`.
- If the operator edits the arguments during approval, the edited arguments are the
  truth from then on.

## 8. When the definition does not cover it

You will hit situations the author did not foresee. For example: unexpected output, an
extra resource, a step that is impossible on this platform, or a "done" state that
contradicts the definition.

- **Read-only investigation is fine.** It is good practice to gather a few facts so the
  question you ask is useful.
- **Unplanned changes are not fine.** Do not fix, clean up, upgrade or "improve" things
  the definition does not ask for, however obvious the fix looks.
- Stop and ask the operator with a short summary: what you expected, what you observed,
  and two or three concrete options. If you cannot continue without an answer, call
  `update_workflow(status="blocked")` first.
