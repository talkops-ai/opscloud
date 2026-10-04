#!/usr/bin/env python3
"""Skill Initializer - Creates a new skill from template.

Usage:
 init_skill.py <skill-name> --path <path>

Examples:
 init_skill.py my-new-skill --path skills/public
 init_skill.py my-api-helper --path skills/private
 init_skill.py custom-skill --path /custom/location

For opscloud CLI:
 init_skill.py my-skill --path "${OPSCLOUD_HOME:-$HOME/.opscloud}/skills"
 init_skill.py my-skill --path .opscloud/skills
"""

import sys
from pathlib import Path

MAX_SKILL_NAME_LENGTH = 64

SKILL_TEMPLATE = """---
name: {skill_name}
description: "[TODO: Complete and informative explanation of what the skill does and when to use it. Include WHEN to use this skill - specific scenarios, file types, or tasks that trigger it.]"
---

# {skill_title}

## Overview

[TODO: 1-2 sentences explaining what this skill enables]

## Structuring This Skill

[TODO: Choose the structure that best fits this skill's purpose. Common patterns:

**1. Workflow-Based** (best for sequential processes)
- Works well when there are clear step-by-step procedures
- Example: Cloud deployment with "Pre-flight Checks" → "Terraform Plan" → "Apply" → "Verification"
- Structure: ## Overview → ## Workflow Decision Tree → ## Step 1 → ## Step 2...

**2. Task-Based** (best for tool collections)
- Works well when the skill offers different operations/capabilities
- Example: Kubernetes skill with "Cluster Health" → "Pod Logs" → "Rollout Restart" → "Config Inspection"
- Structure: ## Overview → ## Quick Start → ## Task Category 1 → ## Task Category 2...

**3. Reference/Guidelines** (best for standards or specifications)
- Works well for security baselines, coding standards, or compliance
- Example: IAM governance with "Least Privilege Baselines" → "Tagging Standards" → "Audit Remediation"
- Structure: ## Overview → ## Guidelines → ## Specifications → ## Usage...

**4. Capabilities-Based** (best for integrated systems)
- Works well when the skill provides multiple interrelated features
- Example: FinOps with "Cost Anomaly Detection" → "Resource Rightsizing" → "Reserved Instance Analysis"
- Structure: ## Overview → ## Core Capabilities → ### 1. Feature → ### 2. Feature...

Patterns can be mixed and matched as needed. Most skills combine patterns (e.g., start with task-based, add workflow for complex operations).

Delete this entire "Structuring This Skill" section when done - it's just guidance.]

## [TODO: Replace with the first main section based on chosen structure]

[TODO: Add content here. See examples in existing skills:
- Code samples for technical skills
- Decision trees for complex workflows
- Concrete examples with realistic user requests
- References to scripts/templates/references as needed]

## Resources

This skill includes example resource directories that demonstrate how to organize different types of bundled resources:

### scripts/
Executable code (Python/Bash/etc.) that can be run directly to perform specific operations.

**Examples from other skills:**
- Cost optimization: `find_idle_resources.py`, `cleanup_unattached_ebs.py` - automation utilities
- Terraform: `generate_state_backup.py`, `check_drift.py` - Python modules for infrastructure inspection

**Appropriate for:** Python scripts, shell scripts, or any executable code that performs automation, data processing, or specific operations.

**Note:** Scripts may be executed without loading into context, but can still be read by the agent for patching or environment adjustments.

### references/
Documentation and reference material intended to be loaded into context to inform the agent's process and thinking.

**Examples from other skills:**
- AWS Architecture: `well_architected_framework.md`, `tagging_guidelines.md` - detailed policy guides
- Infrastructure schemas: API reference documentation and query examples
- Compliance: Security baselines, audit standards

**Appropriate for:** In-depth documentation, API references, database schemas, comprehensive guides, or any detailed information that the agent should reference while working.

### assets/
Files not intended to be loaded into context, but rather used within the output the agent produces.

**Examples from other skills:**
- Infrastructure: Terraform templates (.tf), Dockerfiles, Kubernetes manifests (.yaml)
- Configuration: Helm values templates, CI/CD pipeline definitions (.github/workflows)
- Boilerplate code: Project starter directories

**Appropriate for:** Templates, boilerplate code, document templates, configuration samples, or any files meant to be copied or used in the final output.

---

**Any unneeded directories can be deleted.** Not every skill requires all three types of resources.
"""

EXAMPLE_SCRIPT = '''#!/usr/bin/env python3
"""
Example helper script for {skill_name}

This is a placeholder script that can be executed directly.
Replace with actual implementation or delete if not needed.

Example real scripts from other skills:
- aws/scripts/find_unattached_volumes.py - Discovers orphaned EBS volumes
- k8s/scripts/check_pod_restarts.py - Analyzes crash loops
"""

def main():
    print("This is an example script for {skill_name}")
    # TODO: Add actual script logic here
    # This could be cloud API calls, data processing, validation, etc.

if __name__ == "__main__":
    main()
'''

EXAMPLE_REFERENCE = """# Reference Documentation for {skill_title}

This is a placeholder for detailed reference documentation.
Replace with actual reference content or delete if not needed.

Example real reference docs from other skills:
- aws/references/iam_policies.md - Comprehensive guide for least privilege IAM policies
- finops/references/pricing_models.md - Deep-dive on savings plans and spot pricing
- terraform/references/state_locking.md - Guide for DynamoDB backend lock management

## When Reference Docs Are Useful

Reference docs are ideal for:
- Comprehensive API documentation
- Detailed workflow guides
- Complex multi-step processes
- Information too lengthy for main SKILL.md
- Content that's only needed for specific use cases

## Structure Suggestions

### API Reference Example
- Overview
- Authentication
- Endpoints with examples
- Error codes
- Rate limits

### Workflow Guide Example
- Prerequisites
- Step-by-step instructions
- Common patterns
- Troubleshooting
- Best practices
"""

EXAMPLE_ASSET = """# Example Asset File

This placeholder represents where asset files would be stored.
Replace with actual asset files (templates, manifests, boilerplates, etc.) or delete if not needed.

Asset files are NOT intended to be loaded into context, but rather used within
the output the agent produces.

Example asset files from other skills:
- Terraform modules: main.tf, variables.tf, outputs.tf
- Kubernetes: deployment-template.yaml, ingress-template.yaml
- Docker: Dockerfile.base, docker-compose.prod.yml
- CI/CD: github-action-deploy.yml

## Common Asset Types

- Templates: .tf, .yaml, .json, boilerplate directories
- Configuration: .env.example, config.toml
- Scripts/Manifests: helm chart starters
- Boilerplate code: Project directories, starter files

Note: This is a text placeholder. Actual assets can be any file type.
"""


def _validate_name(name: str) -> tuple[bool, str]:
    """Validate skill name per Agent Skills spec.

    Requirements (https://agentskills.io/specification):
    - 1-64 characters
    - Unicode lowercase alphanumeric and hyphens only
    - Cannot start or end with hyphen
    - No consecutive hyphens

    Unicode lowercase alphanumeric means any character where
    `c.isalpha() and c.islower()` or `c.isdigit()` returns `True`.

    Args:
        name: The skill name to validate.

    Returns:
        Tuple of (is_valid, error_message). If valid, error_message is empty.
    """
    if not name or not name.strip():
        return False, "cannot be empty"
    if len(name) > MAX_SKILL_NAME_LENGTH:
        return False, "cannot exceed 64 characters"
    if name.startswith("-") or name.endswith("-") or "--" in name:
        return False, "must be lowercase alphanumeric with single hyphens only"
    for c in name:
        if c == "-":
            continue
        if (c.isalpha() and c.islower()) or c.isdigit():
            continue
        return False, "must be lowercase alphanumeric with single hyphens only"
    return True, ""


def title_case_skill_name(skill_name: str) -> str:
    """Convert hyphenated skill name to Title Case for display.

    Returns:
        Skill name with each word capitalized.
    """
    return " ".join(word.capitalize() for word in skill_name.split("-"))


def init_skill(skill_name: str, path: str | Path) -> Path | None:
    """Initialize a new skill directory with template SKILL.md.

    Args:
        skill_name: Name of the skill
        path: Path where the skill directory should be created

    Returns:
        Path to created skill directory, or None if error
    """
    is_valid, error_msg = _validate_name(skill_name)
    if not is_valid:
        print(f"Error: Invalid skill name: {error_msg}")
        print(
            "Skill names must be lowercase alphanumeric with hyphens only.\n"
            "Examples: ecs-deploy, terraform-fix, cost-optimizer"
        )
        return None

    # Determine skill directory path
    skill_dir = Path(path).resolve() / skill_name

    # Check if directory already exists
    if skill_dir.exists():
        print(f"Error: Skill directory already exists: {skill_dir}")
        return None

    # Create skill directory
    try:
        skill_dir.mkdir(parents=True, exist_ok=False)
        print(f"Created skill directory: {skill_dir}")
    except Exception as e:
        print(f"Error creating directory: {e}")
        return None

    # Create SKILL.md from template
    skill_title = title_case_skill_name(skill_name)
    skill_content = SKILL_TEMPLATE.format(
        skill_name=skill_name, skill_title=skill_title
    )

    skill_md_path = skill_dir / "SKILL.md"
    try:
        skill_md_path.write_text(skill_content, encoding="utf-8")
        print("Created SKILL.md")
    except Exception as e:
        print(f"Error creating SKILL.md: {e}")
        return None

    # Create resource directories with example files
    try:
        # Create scripts/ directory with example script
        scripts_dir = skill_dir / "scripts"
        scripts_dir.mkdir(exist_ok=True)
        example_script = scripts_dir / "example.py"
        example_script.write_text(
            EXAMPLE_SCRIPT.format(skill_name=skill_name), encoding="utf-8"
        )
        example_script.chmod(0o755)
        print("Created scripts/example.py")

        # Create references/ directory with example reference doc
        references_dir = skill_dir / "references"
        references_dir.mkdir(exist_ok=True)
        example_reference = references_dir / "api_reference.md"
        example_reference.write_text(
            EXAMPLE_REFERENCE.format(skill_title=skill_title), encoding="utf-8"
        )
        print("Created references/api_reference.md")

        # Create assets/ directory with example asset placeholder
        assets_dir = skill_dir / "assets"
        assets_dir.mkdir(exist_ok=True)
        example_asset = assets_dir / "example_asset.txt"
        example_asset.write_text(EXAMPLE_ASSET, encoding="utf-8")
        print("Created assets/example_asset.txt")
    except Exception as e:
        print(f"Error creating resource directories: {e}")
        return None

    # Print next steps
    print(f"\nSkill '{skill_name}' initialized successfully at {skill_dir}")
    print("\nNext steps:")
    print("1. Edit SKILL.md to complete the TODO items and update the description")
    print(
        "2. Customize or delete the example files in scripts/, references/, and assets/"
    )
    print("3. Run the validator when ready to check the skill structure")

    return skill_dir


def main() -> None:
    """Main entry point for the skill initialization script."""
    if len(sys.argv) < 4 or sys.argv[2] != "--path":
        print("Usage: init_skill.py <skill-name> --path <path>")
        print("\nSkill name requirements:")
        print(" - Hyphen-case identifier (e.g., 'ecs-deploy')")
        print(" - Lowercase letters, digits, and hyphens only")
        print(" - Max 64 characters")
        print(" - Must match directory name exactly")
        print("\nExamples:")
        print(" init_skill.py my-new-skill --path skills/public")
        print(" init_skill.py my-api-helper --path skills/private")
        print(" init_skill.py custom-skill --path /custom/location")
        print("\nFor opscloud CLI:")
        skills_dir = "${OPSCLOUD_HOME:-$HOME/.opscloud}/skills"
        print(f' init_skill.py my-skill --path "{skills_dir}"')
        print(' init_skill.py my-skill --path .opscloud/skills')
        sys.exit(1)

    skill_name = sys.argv[1]
    path = sys.argv[3]

    # Early validation for fast feedback
    is_valid, error_msg = _validate_name(skill_name)
    if not is_valid:
        print(f"Error: Invalid skill name '{skill_name}': {error_msg}")
        print("\nSkill name requirements:")
        print(" - Lowercase letters, digits, and hyphens only")
        print(" - Cannot start or end with hyphen")
        print(" - No consecutive hyphens")
        print(" - Max 64 characters")
        sys.exit(1)

    print(f"Initializing skill: {skill_name}")
    print(f"   Location: {path}")
    print()

    result = init_skill(skill_name, path)

    if result:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
