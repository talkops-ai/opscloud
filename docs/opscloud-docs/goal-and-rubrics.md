# Goals and rubrics

> Set goals interactively or grade work automatically in CI/CD

OpsCloud provides two mechanisms to enforce quality and track progress:

- **Goals** — For interactive collaborative sessions. OpsCloud breaks high-level objectives into verifiable acceptance criteria and tracks them in real-time in the TUI.
- **Rubrics** — For automated CI/CD pipelines. A dedicated grader model evaluates the agent's deliverables against your specification and loops on fixes until all criteria pass.

---

## Goals (interactive mode)

Goals work best in interactive sessions when working alongside OpsCloud on multi-step tasks.

### Set a goal

At launch:

```bash
opscloud --goal "Harden production EKS cluster with Pod Security Standards and NetworkPolicies"
```

Or inside an active session:

```text
/goal Implement AWS KMS state encryption and cross-account IAM roles for OpenTofu
```

### How goals work

1. OpsCloud inspects your workspace and generates an interactive checklist of acceptance criteria.
2. The TUI goal widget tracks criteria status live: `[pending]`, `[passed]`, `[failed]`.
3. The agent utilizes built-in tools (`get_goal`, `update_goal`) to record progress as milestones are reached.

---

## Rubrics (autonomous CI/CD mode)

Rubrics provide autonomous quality assurance for non-interactive (`-n`) and CI/CD workflows. The working agent generates code and manifests, a dedicated grader model evaluates the work tree against the rubric, and if any criteria fail, the grader feeds a specific deficiency report back to the agent for self-correction.

### Specify an inline rubric

```bash
opscloud -n "Author a Terraform module for an AWS RDS Aurora Postgres cluster" \
  --rubric "1. Multi-AZ deployment is enabled.
2. Storage is encrypted with AWS KMS customer-managed key.
3. Automated backups are retained for 14 days.
4. Enhanced monitoring and Performance Insights are enabled.
5. Security group restricts port 5432 to VPC CIDR only." \
  --rubric-model "openai:gpt-4o" \
  --rubric-max-iterations 3 \
  -y
```

### Load rubrics from a specification file

For complex architectural standards, load criteria from a markdown file using `@path`:

```bash
opscloud -n "Refactor VPC networking" --rubric @specs/vpc-rubric.md -y
```

**`specs/vpc-rubric.md`:**
```markdown
# VPC Architecture Rubric

1. Primary CIDR block is 10.100.0.0/16 with 3 public and 3 private subnets across 3 AZs.
2. NAT Gateways are deployed redundantly across all 3 availability zones.
3. VPC Flow Logs publish to an encrypted CloudWatch Log Group.
4. Default security group denies all inbound and outbound traffic.
5. VPC gateway endpoints are provisioned for S3 and DynamoDB.
```

---

## The Grader Evaluation Cycle

```
┌────────────────────────────────────────────────────────┐
│                   Rubric Evaluation Loop               │
├────────────────────────────────────────────────────────┤
│ 1. Worker Agent creates code/manifests in workspace    │
│ 2. Grader Model evaluates work tree against rubric     │
│ 3. If PASS ──> Return 0, emit JSON report & exit       │
│ 4. If FAIL ──> Grader feeds back specific deficiency   │
│    report into Worker Agent context                    │
│ 5. Worker iterates on fixes and re-submits to Grader   │
│ 6. Repeats until PASS or max iterations reached        │
└────────────────────────────────────────────────────────┘
```

Using a separate grader model (e.g. evaluating an Anthropic worker agent with an OpenAI GPT-4o grader) eliminates self-evaluation bias and catches subtle hallucinations.

---

## Rubric command-line options

| Flag | What it does |
|---|---|
| `--rubric TEXT\|@PATH` | Acceptance criteria (inline text or `@path` to a file) |
| `--rubric-model MODEL` | Dedicated grader model specifier (e.g. `--rubric-model openai:gpt-4o`) |
| `--rubric-max-iterations N` | Maximum fix-and-recheck iterations (default: 3) |

---

## Goals vs. rubrics comparison

| Feature | Goals | Rubrics |
|---|---|---|
| **Primary Use Case** | Interactive terminal sessions | Autonomous CI/CD pipelines |
| **Criteria Source** | Auto-generated from your prompt | Explicitly defined by you |
| **Execution Loop** | Human guides the agent with real-time feedback | Autonomous grader evaluation loop |
| **Grader Model** | Same model | Dedicated grader model (optional) |
| **CLI Flag** | `--goal TEXT` | `--rubric TEXT\|@PATH` |

---

## CI/CD integration examples

### GitHub Actions: Kubernetes manifest compliance

```bash
opscloud -n "Audit and fix deployment.yaml" \
  --rubric "1. Non-root securityContext is enforced.
2. Read-only root filesystem is enabled.
3. Liveness and readiness probes have timeout thresholds.
4. CPU and memory limits and requests are defined." \
  --rubric-model "openai:gpt-4o" \
  --rubric-max-iterations 3 \
  --quiet \
  -y
```

### Jenkins: Automated Terraform refactoring

```bash
opscloud -n "Refactor database module to support read replicas" \
  --rubric @specs/db-replica-rubric.md \
  --rubric-model "anthropic:claude-3-7-sonnet-20250219" \
  --max-turns 15 \
  --quiet \
  -y
```
