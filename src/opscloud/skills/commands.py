"""CLI commands for skill management."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
from typing import TYPE_CHECKING, Any, assert_never

from rich.console import Console
from rich.markup import escape as escape_markup

from opscloud.config.paths import (
    PATHS,
    display_path,
    ensure_project_skills_dir,
    ensure_user_skills_dir,
    get_built_in_skills_dir,
    get_project_agent_skills_dir,
    get_project_skills_dir,
    get_user_agent_skills_dir,
    get_user_skills_dir,
)
from opscloud.config.settings import get_glyphs, settings
from opscloud.output import OutputFormat, add_json_output_arg, write_json
from opscloud.ui.theme import DC_CYAN, DC_GREEN, DC_MUTED, DC_PURPLE, DC_TEAL

if TYPE_CHECKING:
    from collections.abc import Callable

    from deepagents.middleware.skills import SkillMetadata

console = Console()

PRIMARY = DC_TEAL
MUTED = DC_MUTED
MAX_SKILL_NAME_LENGTH = 64


def _validate_name(name: str) -> tuple[bool, str]:
    """Validate name per Agent Skills spec.

    Requirements:
    - Max 64 characters
    - Unicode lowercase alphanumeric and hyphens only
    - Cannot start or end with hyphen
    - No consecutive hyphens
    - No path traversal sequences
    """
    if not name or not name.strip():
        return False, "cannot be empty"

    if len(name) > MAX_SKILL_NAME_LENGTH:
        return False, "cannot exceed 64 characters"

    if ".." in name or "/" in name or "\\" in name:
        return False, "cannot contain path components"

    if name.startswith("-") or name.endswith("-") or "--" in name:
        return (
            False,
            "must be lowercase alphanumeric with single hyphens only",
        )

    for c in name:
        if c == "-":
            continue
        if (c.isalpha() and c.islower()) or c.isdigit():
            continue
        return (
            False,
            "must be lowercase alphanumeric with single hyphens only",
        )

    return True, ""


def _validate_skill_path(skill_dir: Path, base_dir: Path) -> tuple[bool, str]:
    """Validate that the resolved skill directory is within the base directory."""
    try:
        resolved_skill = skill_dir.resolve()
        resolved_base = base_dir.resolve()
        if not resolved_skill.is_relative_to(resolved_base):
            return False, f"Skill directory must be within {base_dir}"
    except (OSError, RuntimeError) as e:
        return False, f"Invalid path: {e}"
    else:
        return True, ""


def _format_info_fields(skill: SkillMetadata | dict[str, Any]) -> list[tuple[str, str]]:
    """Extract non-empty optional metadata fields for display."""
    fields: list[tuple[str, str]] = []
    license_val = skill.get("license")
    if license_val:
        fields.append(("License", str(license_val)))
    compat_val = skill.get("compatibility")
    if compat_val:
        fields.append(("Compatibility", str(compat_val)))
    if skill.get("allowed_tools"):
        fields.append(
            ("Allowed Tools", ", ".join(str(t) for t in skill["allowed_tools"]))
        )
    meta = skill.get("metadata")
    if meta and isinstance(meta, dict):
        formatted = ", ".join(f"{k}={v}" for k, v in meta.items())
        fields.append(("Metadata", formatted))
    return fields


def _list(
    agent: str = "opscloud",
    *,
    project: bool = False,
    output_format: OutputFormat = "text",
) -> None:
    """List all available skills for the specified agent."""
    from opscloud.skills.load import list_skills

    effective_project_root = settings.project_root
    user_skills_dir = get_user_skills_dir(agent)
    project_skills_dir = get_project_skills_dir(effective_project_root)
    user_agent_skills_dir = get_user_agent_skills_dir()
    project_agent_skills_dir = get_project_agent_skills_dir(effective_project_root)

    if project:
        if not project_skills_dir:
            if output_format == "json":
                write_json("skills list", [])
                return
            console.print("[yellow]Not in a project directory.[/yellow]")
            console.print(
                "[dim]Project skills require a project root or .opscloud directory.[/dim]",
                style=MUTED,
            )
            return

        has_opscloud_skills = project_skills_dir.exists() and any(project_skills_dir.iterdir())
        has_agent_skills = (
            project_agent_skills_dir is not None
            and project_agent_skills_dir.exists()
            and any(project_agent_skills_dir.iterdir())
        )

        if not has_opscloud_skills and not has_agent_skills:
            if output_format == "json":
                write_json("skills list", [])
                return
            project_skills_display = escape_markup(display_path(project_skills_dir))
            console.print("[yellow]No project skills found.[/yellow]")
            console.print(
                f"[dim]Project skills will be created in {project_skills_display} "
                "when you add them.[/dim]",
                style=MUTED,
            )
            console.print(
                "\n[dim]Create a project skill:\n"
                "  opscloud skills create my-skill --project[/dim]",
                style=MUTED,
            )
            return

        skills = list_skills(
            built_in_skills_dir=None,
            user_skills_dir=None,
            project_skills_dir=project_skills_dir,
            user_agent_skills_dir=None,
            project_agent_skills_dir=project_agent_skills_dir,
            project_root=effective_project_root,
        )

        if output_format == "json":
            write_json("skills list", [dict(s) for s in skills])
            return

        console.print(f"\n[bold {PRIMARY}]Project Skills:[/bold {PRIMARY}]\n")
    else:
        skills = list_skills(
            built_in_skills_dir=get_built_in_skills_dir(),
            user_skills_dir=user_skills_dir,
            project_skills_dir=project_skills_dir,
            user_agent_skills_dir=user_agent_skills_dir,
            project_agent_skills_dir=project_agent_skills_dir,
            project_root=effective_project_root,
        )

        if output_format == "json":
            write_json("skills list", [dict(s) for s in skills])
            return

        if not skills:
            user_skills_display = escape_markup(display_path(user_skills_dir))
            console.print()
            console.print("[yellow]No skills found.[/yellow]")
            console.print()
            console.print(
                "[dim]Skills are loaded from these directories "
                "(highest precedence first):\n"
                "  1. .agents/skills/                 project skills\n"
                "  2. .opscloud/skills/               project skills (alias)\n"
                "  3. ~/.agents/skills/               user skills\n"
                f"  4. {user_skills_display}   user skills (alias)\n"
                "  5. <package>/built_in_skills/      built-in skills[/dim]",
                style=MUTED,
            )
            console.print(
                "\n[dim]Create your first skill:\n  opscloud skills create my-skill[/dim]",
                style=MUTED,
            )
            return

        console.print(f"\n[bold {PRIMARY}]Available Skills:[/bold {PRIMARY}]\n")

    user_skills = [s for s in skills if s.get("source") == "user"]
    project_skills_list = [s for s in skills if s.get("source") == "project"]
    plugin_skills_list = [s for s in skills if s.get("source") == "plugin"]
    built_in_skills_list = [s for s in skills if s.get("source") == "built-in"]

    bullet = get_glyphs().bullet

    if user_skills and not project:
        console.print(f"[bold {DC_CYAN}]User Skills:[/bold {DC_CYAN}]")
        for skill in user_skills:
            skill_path = Path(skill["path"])
            name = escape_markup(skill["name"])
            console.print(f"  {bullet} [bold]{name}[/bold]")
            console.print(
                f"    {escape_markup(display_path(skill_path.parent))}/",
                style=MUTED,
            )
            console.print()
            console.print(
                f"    {escape_markup(skill.get('description', ''))}",
                style=MUTED,
            )
            console.print()

    if project_skills_list:
        if not project and user_skills:
            console.print()
        console.print(f"[bold {DC_GREEN}]Project Skills:[/bold {DC_GREEN}]")
        for skill in project_skills_list:
            skill_path = Path(skill["path"])
            name = escape_markup(skill["name"])
            console.print(f"  {bullet} [bold]{name}[/bold]")
            console.print(
                f"    {escape_markup(display_path(skill_path.parent))}/",
                style=MUTED,
            )
            console.print()
            console.print(
                f"    {escape_markup(skill.get('description', ''))}",
                style=MUTED,
            )
            console.print()

    if plugin_skills_list and not project:
        if user_skills or project_skills_list:
            console.print()
        console.print(f"[bold {DC_PURPLE}]Plugin Skills:[/bold {DC_PURPLE}]")
        for skill in plugin_skills_list:
            skill_path = Path(skill["path"])
            name = escape_markup(skill["name"])
            console.print(f"  {bullet} [bold]{name}[/bold]")
            console.print(
                f"    {escape_markup(display_path(skill_path.parent))}/",
                style=MUTED,
            )
            console.print()
            console.print(
                f"    {escape_markup(skill.get('description', ''))}",
                style=MUTED,
            )
            console.print()

    if built_in_skills_list and not project:
        if user_skills or project_skills_list or plugin_skills_list:
            console.print()
        console.print(f"[bold {DC_PURPLE}]Built-in Skills:[/bold {DC_PURPLE}]")
        for skill in built_in_skills_list:
            name = escape_markup(skill["name"])
            console.print(f"  {bullet} [bold]{name}[/bold]")
            console.print()
            console.print(
                f"    {escape_markup(skill.get('description', ''))}",
                style=MUTED,
            )
            console.print()


def _generate_template(skill_name: str) -> str:
    """Generate a `SKILL.md` template for a new skill per Agent Skills spec."""
    title = skill_name.title().replace("-", " ")
    description = (
        "TODO: Explain what this skill does and when to use it. "
        "Include specific triggers — scenarios, file types, CLI commands, or phrases "
        "that should activate this skill. Example: 'Diagnose and remediate AWS ECS "
        "task crash loops. Use when the user asks about ECS task failures or service instability.'"
    )
    return f"""---
name: {skill_name}
description: "{description}"
# Optional fields per Agent Skills spec:
# license: Apache-2.0
# compatibility: Designed for OpsCloud
# metadata:
#   author: your-org
#   version: "1.0"
# allowed-tools: execute read_file write_file
---

# {title}

## Overview

[TODO: 1-2 sentences explaining what this skill enables]

## Instructions

### Step 1: [First Action]
[Explain what to inspect or run first]

### Step 2: [Second Action]
[Explain analysis or mutation steps]

### Step 3: [Final Action]
[Explain validation and verification steps]

## Best Practices

- [Best practice 1]
- [Best practice 2]
- [Best practice 3]

## Examples

### Example 1: [Scenario Name]

**User Request:** "[Example user request]"

**Approach:**
1. [Step-by-step breakdown]
2. [Using tools and commands]
3. [Expected outcome]
"""


def _create(
    skill_name: str,
    agent: str = "opscloud",
    project: bool = False,
    *,
    output_format: OutputFormat = "text",
) -> None:
    """Create a new skill with a template SKILL.md file."""
    is_valid, error_msg = _validate_name(skill_name)
    if not is_valid:
        console.print(f"[bold red]Error:[/bold red] Invalid skill name: {error_msg}")
        console.print(
            "[dim]Per Agent Skills spec: names must be lowercase alphanumeric "
            "with hyphens only.\n"
            "Examples: ecs-debugger, terraform-state-fix, cost-optimizer[/dim]",
            style=MUTED,
        )
        raise SystemExit(1)

    effective_project_root = settings.project_root
    if project:
        if not effective_project_root:
            console.print("[bold red]Error:[/bold red] Not in a project directory.")
            raise SystemExit(1)
        skills_dir = ensure_project_skills_dir(effective_project_root)
        if skills_dir is None:
            console.print(
                "[bold red]Error:[/bold red] Could not create project skills directory."
            )
            raise SystemExit(1)
    else:
        skills_dir = ensure_user_skills_dir(agent)

    skill_dir = skills_dir / skill_name

    is_valid_path, path_error = _validate_skill_path(skill_dir, skills_dir)
    if not is_valid_path:
        console.print(f"[bold red]Error:[/bold red] {path_error}")
        raise SystemExit(1)

    if skill_dir.exists():
        if output_format == "json":
            write_json(
                "skills create",
                {
                    "name": skill_name,
                    "path": str(skill_dir),
                    "project": project,
                    "already_existed": True,
                },
            )
            return
        console.print(
            f"Skill '{skill_name}' already exists at {display_path(skill_dir)}",
            style=MUTED,
        )
        return

    skill_dir.mkdir(parents=True, exist_ok=True)
    template = _generate_template(skill_name)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(template, encoding="utf-8")

    if output_format == "json":
        write_json(
            "skills create",
            {
                "name": skill_name,
                "path": str(skill_dir),
                "project": project,
            },
        )
        return

    checkmark = get_glyphs().checkmark
    skill_dir_display = escape_markup(display_path(skill_dir))
    skill_md_display = escape_markup(display_path(skill_md))
    console.print(
        f"\n[bold {PRIMARY}]{checkmark} Skill '{skill_name}' created successfully![/bold {PRIMARY}]"
    )
    console.print(f"Location: {skill_dir_display}\n", style=MUTED)
    console.print(
        "[dim]Edit the SKILL.md file to customize:\n"
        "  1. Update the description in YAML frontmatter with activation triggers\n"
        "  2. Fill in instructions, best practices, and examples\n"
        "  3. Add any supporting files (scripts, configs, templates)\n"
        "\n"
        f"  nano {skill_md_display}\n",
        style=MUTED,
    )


def _info(
    skill_name: str,
    *,
    agent: str = "opscloud",
    project: bool = False,
    output_format: OutputFormat = "text",
) -> None:
    """Show detailed information about a specific skill."""
    from opscloud.skills.load import list_skills

    effective_project_root = settings.project_root
    user_skills_dir = get_user_skills_dir(agent)
    project_skills_dir = get_project_skills_dir(effective_project_root)
    user_agent_skills_dir = get_user_agent_skills_dir()
    project_agent_skills_dir = get_project_agent_skills_dir(effective_project_root)

    if project:
        if not project_skills_dir:
            console.print("[bold red]Error:[/bold red] Not in a project directory.")
            raise SystemExit(1)
        skills = list_skills(
            built_in_skills_dir=None,
            user_skills_dir=None,
            project_skills_dir=project_skills_dir,
            user_agent_skills_dir=None,
            project_agent_skills_dir=project_agent_skills_dir,
            project_root=effective_project_root,
        )
    else:
        skills = list_skills(
            built_in_skills_dir=get_built_in_skills_dir(),
            user_skills_dir=user_skills_dir,
            project_skills_dir=project_skills_dir,
            user_agent_skills_dir=user_agent_skills_dir,
            project_agent_skills_dir=project_agent_skills_dir,
            project_root=effective_project_root,
        )

    normalized = skill_name.strip().lower()
    skill = next(
        (
            s for s in skills
            if s.get("name", "").lower() == normalized
            or (":" in s.get("name", "") and s.get("name", "").rsplit(":", 1)[-1].lower() == normalized)
        ),
        None,
    )

    if not skill:
        console.print(f"[bold red]Error:[/bold red] Skill '{skill_name}' not found.")
        console.print("\n[dim]Available skills:[/dim]", style=MUTED)
        for s in skills:
            console.print(f"  - {s['name']}", style=MUTED)
        raise SystemExit(1)

    if output_format == "json":
        write_json("skills info", dict(skill))
        return

    skill_path = Path(skill["path"])
    try:
        skill_content = skill_path.read_text(encoding="utf-8")
    except Exception:
        skill_content = "(Unable to read SKILL.md content)"

    source_labels = {
        "project": ("Project Skill", DC_GREEN),
        "user": ("User Skill", DC_CYAN),
        "plugin": ("Plugin Skill", DC_PURPLE),
        "built-in": ("Built-in Skill", DC_PURPLE),
    }
    source_label, source_color = source_labels.get(skill.get("source", ""), ("Skill", MUTED))

    shadowed_user_skill = False
    if skill.get("source") == "project" and not project:
        try:
            user_only = list_skills(
                built_in_skills_dir=None,
                user_skills_dir=user_skills_dir,
                project_skills_dir=None,
                user_agent_skills_dir=user_agent_skills_dir,
                project_agent_skills_dir=None,
            )
            shadowed_user_skill = any(s["name"] == skill["name"] for s in user_only)
        except Exception:
            pass

    console.print(
        f"\n[bold {PRIMARY}]Skill: {escape_markup(skill['name'])}[/bold {PRIMARY}] "
        f"[bold {source_color}]({source_label})[/bold {source_color}]\n"
    )
    if shadowed_user_skill:
        console.print(
            f"[yellow]Note: Overrides user skill '{escape_markup(skill['name'])}' "
            "of the same name[/yellow]\n"
        )
    console.print(
        f"[bold]Location:[/bold] {escape_markup(display_path(skill_path.parent))}/\n",
        style=MUTED,
    )
    console.print(
        f"[bold]Description:[/bold] {escape_markup(skill.get('description', ''))}\n",
        style=MUTED,
    )

    for label, value in _format_info_fields(skill):
        console.print(
            f"[bold]{label}:[/bold] {escape_markup(value)}\n",
            style=MUTED,
        )

    skill_dir = skill_path.parent
    try:
        supporting_files = [f for f in skill_dir.iterdir() if f.name != "SKILL.md"]
    except OSError:
        supporting_files = []

    if supporting_files:
        console.print("[bold]Supporting Files:[/bold]", style=MUTED)
        for file in supporting_files:
            console.print(f"  - {escape_markup(file.name)}", style=MUTED)
        console.print()

    console.print(f"[bold {PRIMARY}]Full SKILL.md Content:[/bold {PRIMARY}]\n")
    console.print(skill_content, style=MUTED)
    console.print()


def _delete(
    skill_name: str,
    *,
    agent: str = "opscloud",
    project: bool = False,
    force: bool = False,
    dry_run: bool = False,
    output_format: OutputFormat = "text",
) -> None:
    """Delete a skill directory after validation and confirmation."""
    from opscloud.skills.load import list_skills

    is_valid, error_msg = _validate_name(skill_name)
    if not is_valid:
        console.print(f"[bold red]Error:[/bold red] Invalid skill name: {error_msg}")
        raise SystemExit(1)

    effective_project_root = settings.project_root
    user_skills_dir = get_user_skills_dir(agent)
    project_skills_dir = get_project_skills_dir(effective_project_root)
    user_agent_skills_dir = get_user_agent_skills_dir()
    project_agent_skills_dir = get_project_agent_skills_dir(effective_project_root)

    if project:
        if not project_skills_dir:
            console.print("[bold red]Error:[/bold red] Not in a project directory.")
            raise SystemExit(1)
        skills = list_skills(
            built_in_skills_dir=None,
            user_skills_dir=None,
            project_skills_dir=project_skills_dir,
            user_agent_skills_dir=None,
            project_agent_skills_dir=project_agent_skills_dir,
            project_root=effective_project_root,
        )
    else:
        skills = list_skills(
            built_in_skills_dir=None,
            user_skills_dir=user_skills_dir,
            project_skills_dir=project_skills_dir,
            user_agent_skills_dir=user_agent_skills_dir,
            project_agent_skills_dir=project_agent_skills_dir,
            project_root=effective_project_root,
        )

    normalized = skill_name.strip().lower()
    skill = next((s for s in skills if s["name"].lower() == normalized), None)

    if not skill:
        console.print(f"[bold red]Error:[/bold red] Skill '{skill_name}' not found.")
        console.print("\n[dim]Available skills:[/dim]", style=MUTED)
        for s in skills:
            source_tag = f"[{s.get('source', '')}]"
            console.print(f"  - {s['name']} {source_tag}", style=MUTED)
        raise SystemExit(1)

    source = str(skill.get("source", ""))
    if source == "built-in":
        console.print("[bold red]Error:[/bold red] Cannot delete built-in skills.")
        raise SystemExit(1)

    skill_path = Path(skill["path"])
    skill_dir = skill_path.parent

    base_dir = project_skills_dir if source == "project" else user_skills_dir
    if not base_dir:
        console.print("[bold red]Error:[/bold red] Cannot determine base skills directory.")
        raise SystemExit(1)
    is_valid_path, path_error = _validate_skill_path(skill_dir, base_dir)
    if not is_valid_path:
        console.print(f"[bold red]Error:[/bold red] {path_error}")
        raise SystemExit(1)

    if dry_run:
        if output_format == "json":
            write_json(
                "skills delete",
                {
                    "name": skill_name,
                    "path": str(skill_dir),
                    "dry_run": True,
                },
            )
            return
        console.print(f"Would delete skill '{skill_name}' at {display_path(skill_dir)}")
        console.print("No changes made.", style=MUTED)
        return

    if output_format != "json":
        source_label = "Project Skill" if source == "project" else "User Skill"
        source_color = DC_GREEN if source == "project" else DC_CYAN
        try:
            file_count = sum(1 for f in skill_dir.rglob("*") if f.is_file())
        except OSError:
            file_count = -1

        console.print(
            f"\n[bold {PRIMARY}]Skill:[/bold {PRIMARY}] {escape_markup(skill_name)}"
            f" [bold {source_color}]({source_label})[/bold {source_color}]"
        )
        console.print(f"[bold]Location:[/bold] {escape_markup(display_path(skill_dir))}/", style=MUTED)
        if file_count >= 0:
            console.print(f"[bold]Files:[/bold] {file_count} file(s) will be deleted\n", style=MUTED)

    if not force and output_format != "json":
        console.print(
            "[yellow]Are you sure you want to delete this skill? (y/N)[/yellow] ",
            end="",
        )
        try:
            response = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Cancelled.[/dim]")
            return

        if response not in {"y", "yes"}:
            console.print("[dim]Cancelled.[/dim]")
            return

    if skill_dir.is_symlink():
        console.print(
            "[bold red]Error:[/bold red] Skill directory is a symlink. Refusing to delete for safety."
        )
        raise SystemExit(1)

    is_valid_path, path_error = _validate_skill_path(skill_dir, base_dir)
    if not is_valid_path:
        console.print(f"[bold red]Error:[/bold red] {path_error}")
        raise SystemExit(1)

    try:
        shutil.rmtree(skill_dir)
    except OSError as e:
        console.print(f"[bold red]Error:[/bold red] Failed to fully delete skill: {e}")
        raise SystemExit(1) from e

    if output_format == "json":
        write_json(
            "skills delete",
            {
                "name": skill_name,
                "path": str(skill_dir),
                "deleted": True,
            },
        )
        return

    checkmark = get_glyphs().checkmark
    console.print(
        f"{checkmark} Skill '{skill_name}' deleted successfully!",
        style=f"bold {PRIMARY}",
    )


def _trust(args: argparse.Namespace) -> None:
    """Handle `skills trust list|revoke|clear`."""
    from opscloud.skills.trust import (
        RevokeResult,
        clear_trusted_skill_dirs,
        list_trusted_skill_dir_entries,
        revoke_skill_dir_trust,
    )

    command = getattr(args, "trust_command", None)
    output_format: OutputFormat = (
        "json" if getattr(args, "output_format", "text") == "json" else "text"
    )
    checkmark = get_glyphs().checkmark

    if command in {"list", "ls"}:
        try:
            entries = list_trusted_skill_dir_entries(strict=True)
        except (OSError, ValueError) as exc:
            console.print(
                f"[bold red]Error:[/bold red] Could not read the skill trust store: {escape_markup(str(exc))}"
            )
            raise SystemExit(1) from exc

        if output_format == "json":
            write_json(
                "skills trust list",
                [{"dir": path, "trusted_at": trusted_at} for path, trusted_at in entries],
            )
            return

        if not entries:
            console.print()
            console.print("[yellow]No trusted skill directories.[/yellow]")
            console.print(
                "[dim]Directories are trusted when you approve a skill that "
                "resolves outside the standard skill roots.[/dim]",
                style=MUTED,
            )
            console.print()
            return

        console.print(f"\n[bold {PRIMARY}]Trusted skill directories:[/bold {PRIMARY}]\n")
        for path, trusted_at in entries:
            console.print(f"  {escape_markup(display_path(path))}")
            if trusted_at:
                console.print(f"    [dim]trusted {escape_markup(trusted_at)}[/dim]", style=MUTED)
        console.print()

    elif command == "revoke":
        target = args.dir
        result = revoke_skill_dir_trust(target)
        if result is RevokeResult.ERROR:
            console.print(
                f"[bold red]Error:[/bold red] Could not revoke trust for: {escape_markup(str(target))}"
            )
            raise SystemExit(1)

        if output_format == "json":
            write_json("skills trust revoke", {"dir": str(target), "result": result.value})
            return

        match result:
            case RevokeResult.REMOVED:
                console.print(
                    f"{checkmark} Revoked trust for: {escape_markup(str(target))}",
                    style=f"bold {PRIMARY}",
                )
            case RevokeResult.NOT_FOUND:
                console.print(f"[yellow]No trust entry found for:[/yellow] {escape_markup(str(target))}")
            case _:
                assert_never(result)

    elif command == "clear":
        if not clear_trusted_skill_dirs():
            console.print("[bold red]Error:[/bold red] Could not clear trusted directories.")
            raise SystemExit(1)

        if output_format == "json":
            write_json("skills trust clear", {"cleared": True})
            return

        console.print(
            f"{checkmark} Cleared all trusted skill directories.",
            style=f"bold {PRIMARY}",
        )
    else:
        from opscloud.ui import ui_help

        ui_help.show_skills_trust_help()


def setup_skills_parser(
    subparsers: Any,
    *,
    make_help_action: Callable[[Callable[[], None]], type[argparse.Action]] | None = None,
    add_output_args: Callable[[argparse.ArgumentParser], None] | None = None,
) -> argparse.ArgumentParser:
    """Setup the skills subcommand parser with all subcommands."""

    def _lazy_help(fn_name: str) -> Callable[[], None]:
        def _show() -> None:
            from opscloud.ui import ui_help

            fn = getattr(ui_help, fn_name, None)
            if fn:
                fn()
            else:
                ui_help.show_skills_help()

        return _show

    def help_parent(help_fn: Callable[[], None]) -> list[argparse.ArgumentParser]:
        if make_help_action is None:
            return []
        parent = argparse.ArgumentParser(add_help=False)
        parent.add_argument("-h", "--help", action=make_help_action(help_fn))
        return [parent]

    skills_parser = subparsers.add_parser(
        "skills",
        aliases=["skill"],
        help="Manage agent skills",
        description="Manage agent skills - list, create, view, and delete skills.",
        add_help=make_help_action is None,
        parents=help_parent(_lazy_help("show_skills_help")),
    )
    if add_output_args is not None:
        add_output_args(skills_parser)
    else:
        add_json_output_arg(skills_parser)

    skills_subparsers = skills_parser.add_subparsers(
        dest="skills_command", help="Skills command"
    )

    # 1. list / ls
    list_parser = skills_subparsers.add_parser(
        "list",
        aliases=["ls"],
        help="List all available skills",
        description="List skills from user, project, plugin, and built-in directories.",
        add_help=make_help_action is None,
        parents=help_parent(_lazy_help("show_skills_list_help")),
    )
    if add_output_args is not None:
        add_output_args(list_parser)
    else:
        add_json_output_arg(list_parser)
    list_parser.add_argument(
        "--agent",
        default="opscloud",
        help="Agent identifier for skills (default: opscloud)",
    )
    list_parser.add_argument(
        "--project",
        action="store_true",
        help="Show only project-level skills",
    )

    # 2. create
    create_parser = skills_subparsers.add_parser(
        "create",
        help="Create a new skill",
        description="Create a new skill directory with a template SKILL.md file.",
        add_help=make_help_action is None,
        parents=help_parent(_lazy_help("show_skills_create_help")),
    )
    if add_output_args is not None:
        add_output_args(create_parser)
    else:
        add_json_output_arg(create_parser)
    create_parser.add_argument(
        "name",
        help="Name of the skill to create (e.g. eks-debugger)",
    )
    create_parser.add_argument(
        "--agent",
        default="opscloud",
        help="Agent identifier for skills (default: opscloud)",
    )
    create_parser.add_argument(
        "--project",
        action="store_true",
        help="Create skill in project directory instead of user directory",
    )

    # 3. info
    info_parser = skills_subparsers.add_parser(
        "info",
        help="Show detailed information about a skill",
        description="Show detailed information about a specific skill",
        add_help=make_help_action is None,
        parents=help_parent(_lazy_help("show_skills_info_help")),
    )
    if add_output_args is not None:
        add_output_args(info_parser)
    else:
        add_json_output_arg(info_parser)
    info_parser.add_argument("name", help="Name of the skill to show info for")
    info_parser.add_argument(
        "--agent",
        default="opscloud",
        help="Agent identifier for skills (default: opscloud)",
    )
    info_parser.add_argument(
        "--project",
        action="store_true",
        help="Search only in project skills",
    )

    # 4. delete
    delete_parser = skills_subparsers.add_parser(
        "delete",
        aliases=["rm"],
        help="Delete a skill",
        description="Delete a skill directory and all its contents",
        add_help=make_help_action is None,
        parents=help_parent(_lazy_help("show_skills_delete_help")),
    )
    if add_output_args is not None:
        add_output_args(delete_parser)
    else:
        add_json_output_arg(delete_parser)
    delete_parser.add_argument("name", help="Name of the skill to delete")
    delete_parser.add_argument(
        "--agent",
        default="opscloud",
        help="Agent identifier for skills (default: opscloud)",
    )
    delete_parser.add_argument(
        "--project",
        action="store_true",
        help="Search only in project skills",
    )
    delete_parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Skip confirmation prompt",
    )
    delete_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without making changes",
    )

    # 5. trust
    trust_parser = skills_subparsers.add_parser(
        "trust",
        help="Manage trusted skill directories",
        description="List, revoke, or clear trusted skill directories outside standard roots.",
        add_help=make_help_action is None,
        parents=help_parent(_lazy_help("show_skills_trust_help")),
    )
    if add_output_args is not None:
        add_output_args(trust_parser)
    else:
        add_json_output_arg(trust_parser)
    trust_subparsers = trust_parser.add_subparsers(
        dest="trust_command", help="Trust command"
    )
    trust_list = trust_subparsers.add_parser(
        "list",
        aliases=["ls"],
        help="List trusted skill directories",
    )
    add_json_output_arg(trust_list)

    revoke_parser = trust_subparsers.add_parser(
        "revoke",
        help="Revoke trust for a directory",
    )
    revoke_parser.add_argument("dir", help="Directory path to revoke")
    add_json_output_arg(revoke_parser)

    clear_parser = trust_subparsers.add_parser(
        "clear",
        help="Remove all trusted skill directories",
    )
    add_json_output_arg(clear_parser)

    return skills_parser


def execute_skills_command(args: argparse.Namespace) -> None:
    """Execute skills subcommands based on parsed arguments."""
    if args.skills_command == "trust":
        _trust(args)
        return

    agent = getattr(args, "agent", "opscloud") or "opscloud"
    is_valid, error_msg = _validate_name(agent)
    if not is_valid:
        console.print(f"[bold red]Error:[/bold red] Invalid agent name: {error_msg}")
        raise SystemExit(1)

    output_format: OutputFormat = (
        "json" if getattr(args, "output_format", "text") == "json" else "text"
    )

    if args.skills_command in {"list", "ls"}:
        _list(agent=agent, project=getattr(args, "project", False), output_format=output_format)
    elif args.skills_command == "create":
        _create(
            args.name,
            agent=agent,
            project=getattr(args, "project", False),
            output_format=output_format,
        )
    elif args.skills_command == "info":
        _info(
            args.name,
            agent=agent,
            project=getattr(args, "project", False),
            output_format=output_format,
        )
    elif args.skills_command in {"delete", "rm"}:
        _delete(
            args.name,
            agent=agent,
            project=getattr(args, "project", False),
            force=getattr(args, "force", False),
            dry_run=getattr(args, "dry_run", False),
            output_format=output_format,
        )
    else:
        from opscloud.ui import ui_help

        ui_help.show_skills_help()


# Backwards compatibility API
def list_skills_command() -> list[dict[str, Any]]:
    """List all discovered skills and their trust status."""
    from opscloud.skills.load import list_skills
    from opscloud.skills.trust import SkillTrustStore

    discovered = list_skills(project_root=settings.project_root)
    store = SkillTrustStore()

    results = []
    for skill in discovered:
        path_str = skill.get("path", "")
        name_str = skill.get("name", "")
        source_str = skill.get("source", "")
        path = Path(path_str)
        trusted = store.is_trusted(name_str, path)
        results.append(
            {
                "name": name_str,
                "description": skill.get("description", ""),
                "source": source_str,
                "plugin_id": skill.get("plugin_id"),
                "path": str(path),
                "trusted": "Yes" if trusted else "No",
            }
        )
    return results


def trust_skill_command(name: str, path_str: str) -> bool:
    """Trust a skill directory."""
    from opscloud.skills.trust import trust_skill_dir

    path = Path(path_str).resolve()
    if not path.exists():
        return False
    return trust_skill_dir(path)


__all__ = [
    "MAX_SKILL_NAME_LENGTH",
    "execute_skills_command",
    "list_skills_command",
    "setup_skills_parser",
    "trust_skill_command",
]
