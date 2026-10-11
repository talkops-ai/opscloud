---
name: workflow-inputs
description: "How to resolve, validate, and confirm a workflow's inputs before doing any work. Read at the START of every workflow run, and again whenever the operator supplies new values mid-run. Covers parsing operator arguments (key=value, --flags, positional, free text), defaults, allowed values, derived values, secrets, target verification, and the single batched ask_user call."
license: MIT
compatibility: designed for opscloud
metadata:
  workflow: "false"
---

# Resolving workflow inputs

Wrong inputs are the most common cause of a workflow doing the wrong thing to the
wrong target. Resolve every input **before** the first action that changes anything.

## 1. Build the input list from the definition

Read the workflow definition and list every input it needs, explicitly or implicitly:

- **Declared inputs**: frontmatter `parameters`/`inputs`, an "Inputs" or
  "Parameters" section, a usage line, or placeholders (`<env>`, `{{service}}`, `$REGION`).
- **Implicit inputs**: values that steps reference without declaring them. For example,
  "scale the deployment" implies a namespace and a deployment name.
- For each input, note whether it is **required**, its **default**, its **allowed values**,
  and its **format** (for example semver, ISO date, ARN, CIDR, ticket key).

Do not invent inputs the definition does not use.

## 2. Parse the operator arguments

Operators type arguments in many shapes. Accept all of them:

| Form | Example | Interpretation |
|---|---|---|
| `key=value` | `env=prod service=checkout` | Exact assignment |
| Flags | `--env prod`, `--dry-run`, `--no-notify` | Assignment, or a boolean |
| Positional | `checkout prod` | Map in the order the definition lists its inputs, only if unambiguous |
| Free text | `restart checkout in prod-eu` | Extract values; treat anything uncertain as missing |
| Quoted | `message="deploy v2.3 hotfix"` | Keep the spaces; strip the quotes |

Match keys case-insensitively. Accept obvious aliases (`environment`/`env`,
`ns`/`namespace`, `region`/`location`). Never silently drop an argument you could not map.
Mention it in the resolved-input summary as "unused".

## 3. Fill, derive, validate

In this order:

1. **Operator value**: an explicit operator value always wins.
2. **Definition default**: use it, and mark it `(default)`.
3. **Derived value**: use it only when the definition says how, or the derivation is
   deterministic. For example, the cluster comes from an env→cluster table in the
   definition, or the current git branch comes from `git rev-parse`. Mark it `(derived: how)`.
4. **Discovered value**: you may run *read-only* lookups to resolve or validate a value,
   such as listing namespaces, checking that a service exists, or reading the current
   version. Never mutate anything to discover an input.

Then validate each value: allowed values, format, existence of the target, and
consistency between inputs (for example, a region that belongs to the environment,
or a version that exists in the registry). Treat a value that fails validation as missing.

## 4. Ask once, batched

If anything required is missing, ambiguous or invalid, make **one** `ask_user` call
before any action:

- Put every missing or invalid input in the same call. Do not ask one question at a time.
- Use choice options whenever the allowed values are known, or were discovered by a
  read-only lookup.
- Say why each value is needed in a few words, and show what you already resolved so
  the operator can correct it.
- Do not ask for inputs that have defaults, unless the default is dangerous for this
  target (for example, a default `replicas=0` in production).

Never guess these, even when it looks obvious:

- environments, accounts, projects or subscriptions
- clusters, regions, namespaces
- resource identifiers
- versions to deploy or roll back to
- recipients of notifications
- anything irreversible

## 5. Echo the resolved inputs

Before the first step, post a compact table of the resolved inputs:

```
Resolved inputs
  env        prod            (operator)
  service    checkout        (operator)
  namespace  payments        (derived: service catalog)
  replicas   3               (default)
  dry_run    false           (default)
  unused     --verbose
```

This gives the operator a last look without a separate approval step. Approvals for
individual tools still apply.

## 6. Secrets and sensitive values

- Never echo tokens, passwords, keys or connection strings. Show `••••` or the name
  of the secret.
- Prefer references (a secret name, a vault path, an env var name) over literal values.
  If the operator pastes a secret, use it, but do not repeat it in notes, files or
  the final report.
- If a step needs credentials you do not have, the run is `blocked`. Do not try
  alternative credentials.

## 7. Inputs that change mid-run

If the operator gives a new or corrected value during the run:

- Restate which steps already ran with the old value.
- Re-validate the new value.
- Continue only from a step where the change is safe. If completed steps depend on the
  old value, ask whether to redo them.
