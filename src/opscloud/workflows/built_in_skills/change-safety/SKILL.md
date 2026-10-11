---
name: change-safety
description: "Guardrails for workflow steps that modify real systems (infrastructure, deployments, data, access, configuration, external services). Read before the first mutating step of any workflow, and always when the target is production, shared, or customer-facing, or when a step is destructive or irreversible. Covers classifying risk, blast radius, before-state capture, rollback readiness, stop conditions, and data and access hygiene."
license: MIT
compatibility: designed for opscloud
metadata:
  workflow: "false"
---

# Change safety

The operator triggered the workflow, but you are the one pressing the buttons.
Make every change deliberate, scoped and reversible wherever possible.

## 1. Classify each step before running it

| Class | Examples | Handling |
|---|---|---|
| **Read** | describe, get, list, logs, metrics, plan, diff | Run freely |
| **Reversible change** | scale, restart, config change with the old value captured, feature flag, deploy of a known version | Capture the before-state, then run |
| **Hard to reverse** | DB migration, DNS change with long TTL, key rotation, sending notifications, merging, publishing | Confirm that rollback or compensation exists, then run |
| **Irreversible / destructive** | delete, drop, purge, terminate, force-push, revoke, wipe, `terraform destroy` | Run only if the definition explicitly calls for it **and** the target matches the resolved inputs exactly |

The approval policy may gate some of these as well, but your classification decides
how carefully you prepare. It does not decide whether approval is needed.

## 2. Extra care for sensitive targets

Treat a target as sensitive if it is **production**, shared (used by other teams),
customer-facing, regulated, or holds state (databases, storage, queues, secrets).
For sensitive targets:

- Re-verify the active context right before the change, not only during preflight.
  Contexts can switch mid-run.
- Prefer the smallest unit: one instance, pod, region or shard before all of them,
  when the definition allows a canary or progressive rollout.
- Never broaden scope: no wildcards, `--all`, `-A`, empty selectors or account-wide
  operations unless the definition explicitly requires them.
- Respect stated change windows, freezes and required ticket references. If one is
  violated, the run is `blocked`, not "proceed with caution".

## 3. Capture the before-state, know the way back

Before every reversible or harder change, record:

- the exact current value or state, such as an image tag, replica count, config
  snippet, record value or commit SHA;
- the command or procedure that restores it, taken from the definition when it has one.

Keep these in the run notes or in a file. They feed rollback and the final report.
If a change cannot be undone and the definition gives no compensation, mention that
when you announce the step.

## 4. Stop conditions

Stop mutating and reassess (gather evidence, then follow the failure path or ask)
when any of these happens:

- An error rate, latency or health signal gets worse after a change.
- A dry-run or plan shows changes outside the step's scope, such as unexpected
  replacements or deletions, or other resources.
- The observed state contradicts what the definition assumes.
- A command affects a different number of resources than expected
  ("deleted 40 pods" when you expected 4).
- You notice you are acting on a different target than the resolved inputs.

Do not keep running steps just to "get to the end".

## 5. Rollback

- Run a rollback **only** if the definition specifies one for this situation, or the
  operator asks for it.
- If you roll back, verify the restored state with evidence, exactly as you would for
  a forward step.
- If a rollback fails, stop, report precisely which state each resource is in, and
  mark the run `failed`.

## 6. Data and access hygiene

- Do not copy production data to local files, tickets or chat unless the definition
  needs it. When it does, minimise and redact it (tokens, PII, internal hostnames
  when they are not needed).
- Do not create long-lived credentials, widen IAM/RBAC permissions, or open network
  access unless that *is* the workflow's purpose. If temporary access was granted,
  revoke it before finishing, or list it clearly as left in place.
- Leave no stray artifacts on target systems, such as debug pods, temp buckets or
  port-forwards, unless the definition says to keep them.
