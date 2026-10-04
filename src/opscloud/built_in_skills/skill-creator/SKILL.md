---
name: skill-creator
description: "Guide for creating effective skills that extend agent capabilities with specialized knowledge, workflows, or tool integrations. Use this skill when the user asks to: (1) create a new skill, (2) make a skill, (3) build a skill, (4) set up a skill, (5) initialize a skill, (6) scaffold a skill, (7) update or modify an existing skill, (8) validate a skill, (9) learn about skill structure, (10) understand how skills work, or (11) get guidance on skill design patterns. Trigger on phrases like \"create a skill\", \"new skill\", \"make a skill\", \"skill for X\", \"how do I create a skill\", or \"help me build a skill\"."
license: MIT
compatibility: designed for opscloud
---

# Skill Creator

### Skill Location for OpsCloud

The opscloud CLI loads skills from five sources, listed here from lowest to highest precedence:

| # | Directory | Scope | Notes |
|---|-----------|-------|-------|
| 0 | `<package>/built_in_skills/` | Built-in | Ships with opscloud CLI |
| 1 | `$OPSCLOUD_HOME/<agent>/skills/` | User (opscloud default) | Default for `opscloud skills create` (`~/.opscloud/skills/`) |
| 2 | `~/.agents/skills/` | User | Shared across agent tools |
| 3 | `.opscloud/skills/` | Project (opscloud default) | Default for `opscloud skills create --project` |
| 4 | `.agents/skills/` | Project | Shared across agent tools |

`<agent>` is the agent configuration name (default: `opscloud`). When two directories contain a skill with the same name, the higher-precedence version wins — project skills override user skills, and any user or project skill overrides built-in skills.

Example directory layout:

```
$OPSCLOUD_HOME/skills/            # user skills (~/.opscloud/skills/)
├── skill-name-1/
│   └── SKILL.md
└── ...

<project-root>/.opscloud/skills/  # project skills (higher precedence)
├── skill-name-2/
│   └── SKILL.md
└── ...
```

## Core Principles

### Concise is Key

The context window is a public good. Skills share the context window with everything else the agent needs: system prompt, conversation history, other Skills' metadata, and the actual user request.

**Default assumption: The agent is already very capable.** Only add context the agent doesn't already have. Challenge each piece of information: "Does the agent really need this explanation?" and "Does this paragraph justify its token cost?"

Prefer concise examples over verbose explanations.

### Set Appropriate Degrees of Freedom

Match the level of specificity to the task's fragility and variability:

**High freedom (text-based instructions)**: Use when multiple approaches are valid, decisions depend on context, or heuristics guide the approach.

**Medium freedom (pseudocode or scripts with parameters)**: Use when a preferred pattern exists, some variation is acceptable, or configuration affects behavior.

**Low freedom (specific scripts, few parameters)**: Use when operations are fragile and error-prone, consistency is critical, or a specific sequence must be followed.

Think of the agent as exploring a path: a narrow bridge with cliffs needs specific guardrails (low freedom), while an open field allows many routes (high freedom).

### Anatomy of a Skill

Every skill consists of a required SKILL.md file and optional bundled resources:

```
skill-name/
├── SKILL.md (required)
│   ├── YAML frontmatter metadata (required)
│   │   ├── name: (required)
│   │   └── description: (required)
│   └── Markdown instructions (required)
└── Bundled Resources (optional)
    ├── scripts/          - Executable code (Python/Bash/etc.)
    ├── references/       - Documentation intended to be loaded into context as needed
    └── assets/           - Files used in output (templates, icons, fonts, etc.)
```

#### SKILL.md (required)

Every SKILL.md consists of:

- **Frontmatter** (YAML): Contains `name` and `description` fields. These are the only fields that the agent reads to determine when the skill gets used, thus it is very important to be clear and comprehensive in describing what the skill is, and when it should be used.
- **Body** (Markdown): Instructions and guidance for using the skill. Only loaded AFTER the skill triggers (if at all).

#### Bundled Resources (optional)

##### Scripts (`scripts/`)

Executable code (Python/Bash/etc.) for tasks that require deterministic reliability or are repeatedly rewritten.

- **When to include**: When the same code is being rewritten repeatedly or deterministic reliability is needed
- **Example**: `scripts/rotate_pdf.py` for PDF rotation tasks, or `scripts/check_ec2_drift.py` for cloud auditing
- **Benefits**: Token efficient, deterministic, may be executed without loading into context
- **Note**: Scripts may still need to be read by the agent for patching or environment-specific adjustments

##### References (`references/`)

Documentation and reference material intended to be loaded as needed into context to inform the agent's process and thinking.

- **When to include**: For documentation that the agent should reference while working
- **Examples**: `references/terraform_standards.md` for infrastructure schemas, `references/iam_policies.md` for organizational security baselines, `references/api_docs.md` for cloud API specifications
- **Use cases**: Cloud schemas, API documentation, domain knowledge, organizational policies, detailed workflow guides
- **Benefits**: Keeps SKILL.md lean, loaded only when the agent determines it's needed
- **Best practice**: If files are large (>10k words), include search patterns in SKILL.md
- **Avoid duplication**: Information should live in either SKILL.md or references files, not both. Prefer references files for detailed information unless it's truly core to the skill—this keeps SKILL.md lean while making information discoverable without hogging the context window. Keep only essential procedural instructions and workflow guidance in SKILL.md; move detailed reference material, schemas, and examples to references files.

##### Assets (`assets/`)

Files not intended to be loaded into context, but rather used within the output the agent produces.

- **When to include**: When the skill needs files that will be used in the final output
- **Examples**: `assets/terraform-starter/` for boilerplate Terraform code, `assets/docker-compose.yml` for service templates, `assets/k8s-manifest.yaml` for Kubernetes manifests
- **Use cases**: Templates, boilerplate code, images, icons, sample configurations that get copied or modified
- **Benefits**: Separates output resources from documentation, enables the agent to use files without loading them into context

#### What to Not Include in a Skill

A skill should only contain essential files that directly support its functionality. Do NOT create extraneous documentation or auxiliary files, including:

- README.md
- INSTALLATION_GUIDE.md
- QUICK_REFERENCE.md
- CHANGELOG.md
- etc.

The skill should only contain the information needed for an AI agent to do the job at hand. It should not contain auxiliary context about the process that went into creating it, setup and testing procedures, user-facing documentation, etc. Creating additional documentation files just adds clutter and confusion.

### Progressive Disclosure Design Principle

Skills use a three-level loading system to manage context efficiently:

1. **Metadata (name + description)** - Always in context (~100 words)
2. **SKILL.md body** - When skill triggers (<5k words)
3. **Bundled resources** - As needed by the agent (Unlimited because scripts can be executed without reading into context window)

#### Progressive Disclosure Patterns

Keep SKILL.md body to the essentials and under 500 lines to minimize context bloat. SKILL.md files exceeding 10 MB are silently skipped by the agent runtime. Split content into separate files when approaching the line limit. When splitting out content into other files, it is very important to reference them from SKILL.md and describe clearly when to read them, to ensure the reader of the skill knows they exist and when to use them.

**Key principle:** When a skill supports multiple variations, frameworks, or options, keep only the core workflow and selection guidance in SKILL.md. Move variant-specific details (patterns, examples, configuration) into separate reference files.

**Pattern 1: High-level guide with references**

```markdown
# Infrastructure Provisioning

## Quick start

Deploy base resources with Terraform:
[code example]

## Advanced features

- **State management**: See [STATE.md](references/STATE.md) for state locking and migration
- **Multi-region setup**: See [MULTI_REGION.md](references/MULTI_REGION.md) for cross-region disaster recovery
- **Examples**: See [EXAMPLES.md](references/EXAMPLES.md) for common architectural patterns
```

The agent loads STATE.md, MULTI_REGION.md, or EXAMPLES.md only when needed.

**Pattern 2: Domain-specific organization**

For Skills with multiple domains or providers, organize content by domain/provider to avoid loading irrelevant context:

```
cloud-deploy/
├── SKILL.md (workflow + provider selection)
└── references/
    ├── aws.md (AWS deployment patterns)
    ├── gcp.md (GCP deployment patterns)
    └── azure.md (Azure deployment patterns)
```

When the user chooses AWS, the agent only reads `aws.md`.

**Pattern 3: Conditional details**

Show basic content, link to advanced content:

```markdown
# Kubernetes Service Mesh

## Quick Setup

Install default Linkerd mesh profile.

**For Istio multi-cluster**: See [ISTIO.md](references/ISTIO.md)
**For mutual TLS tuning**: See [MTLS.md](references/MTLS.md)
```

The agent reads `ISTIO.md` or `MTLS.md` only when the user needs those features.

**Important guidelines:**

- **Avoid deeply nested references** - Keep references one level deep from SKILL.md. All reference files should link directly from SKILL.md.
- **Structure longer reference files** - For files longer than 100 lines, include a table of contents at the top so the agent can see the full scope when previewing.

## Skill Creation Process

Skill creation involves these steps:

1. Understand the skill with concrete examples
2. Plan reusable skill contents (scripts, references, assets)
3. Initialize the skill (run init_skill.py)
4. Edit the skill (implement resources and write SKILL.md)
5. Validate the skill (run quick_validate.py)
6. Iterate based on real usage

Follow these steps in order, skipping only if there is a clear reason why they are not applicable.

### Step 1: Understanding the Skill with Concrete Examples

Skip this step only when the skill's usage patterns are already clearly understood. It remains valuable even when working with an existing skill.

To create an effective skill, clearly understand concrete examples of how the skill will be used. This understanding can come from either direct user examples or generated examples that are validated with user feedback.

Relevant questions include:
1. **Skill purpose**: What should the skill do?
2. **Triggers/Use cases**: When should it be invoked? What user phrases or tasks trigger it?
3. **Scope**: User-level (`~/.opscloud/skills/`) or project-level (`.opscloud/skills/`)?

To avoid overwhelming users, avoid asking too many questions in a single message. Start with the most important questions and follow up as needed for better effectiveness.

Conclude this step when there is a clear sense of the functionality the skill should support.

### Step 2: Planning the Reusable Skill Contents

To turn concrete examples into an effective skill, analyze each example by:

1. Considering how to execute on the example from scratch
2. Identifying what scripts, references, and assets would be helpful when executing these workflows repeatedly

Example: When building an `ecs-deploy` skill to handle queries like "Deploy this container to ECS":
1. Deploying to ECS requires re-writing task definition JSON and service updates each time
2. A `scripts/deploy_service.py` script would be helpful to store in the skill
3. A `references/task_def_template.json` asset would provide reliable starter configuration

To establish the skill's contents, analyze each concrete example to create a list of the reusable resources to include: scripts, references, and assets.

### Step 3: Initializing the Skill

At this point, it is time to actually create the skill.

Skip this step only if the skill being developed already exists, and iteration or packaging is needed. In this case, continue to the next step.

There are two ways to create a new skill:

#### Option A: `init_skill.py` (recommended for rich skills)

When creating a new skill from scratch, run the `init_skill.py` script located in this skill's `scripts/` directory:

```bash
python <skill-directory>/scripts/init_skill.py <skill-name> --path <output-directory>
```

For opscloud CLI, use any of the skill directories listed in "Skill Location for OpsCloud" above:

```bash
# User skills (default)
python <skill-directory>/scripts/init_skill.py <skill-name> --path "${OPSCLOUD_HOME:-$HOME/.opscloud}/skills"

# Project skills
python <skill-directory>/scripts/init_skill.py <skill-name> --path .opscloud/skills
```

The script:
- Creates the skill directory at the specified path
- Generates a SKILL.md template with proper frontmatter and TODO placeholders
- Creates example resource directories: `scripts/`, `references/`, and `assets/`
- Adds example files in each directory that can be customized or deleted

After initialization, customize or remove the generated SKILL.md and example files as needed.

#### Option B: `opscloud skills create` (quick start)

The built-in CLI command creates a minimal skill with just a `SKILL.md` template — no resource directories. Use this for simple skills that only need instructions and no bundled scripts, references, or assets.

```bash
# Create in user skills directory
opscloud skills create <skill-name>

# Create in project skills directory
opscloud skills create <skill-name> --project
```

Use `init_skill.py` when the skill will include bundled resources (`scripts/`, `references/`, `assets/`). Use `opscloud skills create` for a quick, minimal starting point.

### Step 4: Edit the Skill

When editing the (newly-generated or existing) skill, remember that the skill is being created for an agent to use. Include information that would be beneficial and non-obvious to the agent. Consider what procedural knowledge, domain-specific details, or reusable assets would help the agent execute these tasks more effectively.

#### Learn Proven Design Patterns

Refer to the "Progressive Disclosure Design Principle" and "Core Principles" sections above for established patterns around sequential workflows, conditional logic, and output formatting.

#### Start with Reusable Skill Contents

To begin implementation, start with the reusable resources identified above: `scripts/`, `references/`, and `assets/` files. Note that this step may require user input.

Added scripts must be tested by actually running them to ensure there are no bugs and that the output matches what is expected.

Any example files and directories not needed for the skill should be deleted. The initialization script creates example files in `scripts/`, `references/`, and `assets/` to demonstrate structure, but most skills won't need all of them.

#### Update SKILL.md

**Writing Guidelines:** Always use imperative/infinitive form.

##### Frontmatter

Write the YAML frontmatter with `name` and `description`:

- `name`: The skill name (lowercase alphanumeric with single hyphens, max 64 characters)
- `description`: This is the primary triggering mechanism for your skill, and helps the agent understand when to use the skill.
  - Include both what the Skill does and specific triggers/contexts for when to use it.
  - Include all "when to use" information here - Not in the body. The body is only loaded after triggering, so "When to Use This Skill" sections in the body are not helpful to the agent.
  - Example description: "Manage and optimize AWS ECS clusters, task definitions, and service deployments. Use when the user asks to: (1) deploy a container to ECS, (2) debug failing task health checks, (3) scale ECS services, or (4) inspect cluster capacity."

The only other allowed fields in YAML frontmatter are optional properties per the Agent Skills spec: `license`, `compatibility`, `allowed-tools`, and `metadata`. Do not include any fields beyond these.

##### Body

Write instructions for using the skill and its bundled resources.

### Step 5: Validate the Skill

Once development of the skill is complete, validate it to ensure it meets all requirements:

```bash
python <skill-directory>/scripts/quick_validate.py <path/to/skill-folder>
```

The validation script checks:
- YAML frontmatter format and required fields
- Skill naming conventions (Unicode lowercase alphanumeric with hyphens, max 64 characters)
- Description completeness (no angle brackets, max 1024 characters)
- Required fields: `name` and `description`
- Allowed frontmatter properties only: `name`, `description`, `license`, `compatibility`, `allowed-tools`, `metadata`

If validation fails, fix the reported errors and run the validation command again.

### Step 6: Iterate

After testing the skill, users may request improvements. Often this happens right after using the skill, with fresh context of how the skill performed.

**Iteration workflow:**
1. Use the skill on real tasks
2. Notice struggles or inefficiencies
3. Identify how SKILL.md or bundled resources should be updated
4. Implement changes and test again
