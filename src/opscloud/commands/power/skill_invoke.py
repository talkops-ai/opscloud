"""``/skill:<name>`` — Dynamic skill invocation by name.

Dispatched from the router via prefix matching on ``/skill:``.
"""

from __future__ import annotations

from opscloud.utils.logger import get_logger

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel

logger = get_logger(__name__)


class SkillInvokeHandler(BaseCommandHandler):
    """Load and invoke a discovered skill by name.

    Usage:
      ``/skill:web-research find recent CVEs``
      ``/skill:remember``
      ``/skill:terraform-module create vpc``

    The router dispatches ``/skill:*`` commands here via prefix matching.
    """

    @property
    def name(self) -> str:
        # Special prefix — handled by router startswith() check
        return "/skill:"

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.POWER

    @property
    def safety_level(self) -> SafetyLevel:
        # Untrusted skills may run code — moderate risk
        return SafetyLevel.LOW_RISK

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.QUEUED

    async def execute(self, ctx: CommandContext) -> CommandResult:
        from opscloud.skills.invocation import parse_skill_command

        skill_name, args = parse_skill_command(ctx.raw_command)
        if not skill_name:
            return CommandResult(
                success=False,
                message="Usage: /skill:<name> [args]\n\n"
                "Use `/skills` to see available skills.",
            )

        app = ctx.app
        if app is not None and hasattr(app, "_invoke_skill"):
            await app._invoke_skill(skill_name, args, command=ctx.raw_command)
            return CommandResult(
                success=True,
                message=None,
                mount_as_app_message=False,
            )

        # Resolve skill from registry for CLI / test fallback
        skill = self._resolve_skill(ctx, skill_name)
        if skill is None:
            return CommandResult(
                success=False,
                message=f"Skill `{skill_name}` not found.\n\n"
                "Use `/skills` to see available skills.",
            )

        content = self._load_skill_content(skill)
        if content is None:
            return CommandResult(
                success=False,
                message=f"Could not load SKILL.md for `{skill_name}`.",
            )

        return CommandResult(
            success=True,
            message=f"Skill `{skill_name}` loaded.",
            mount_as_app_message=False,
        )

    def _resolve_skill(self, ctx: CommandContext, name: str) -> dict | None:
        """Resolve a skill by name from the app's discovered skills or direct discovery."""
        name = name.lower()
        app = ctx.app
        if app is not None and hasattr(app, "get_discovered_skills"):
            skills = app.get_discovered_skills()
            if skills:
                for skill in skills:
                    skill_name = str(skill.get("name") or "").lower()
                    if skill_name == name or (":" in skill_name and skill_name.rsplit(":", 1)[-1] == name):
                        return skill

        # Fallback to direct skill loader discovery
        try:
            from pathlib import Path
            from opscloud.config.settings import settings
            from opscloud.skills.loader import list_skills

            built_in_dir = Path(__file__).parent.parent.parent / "built_in_skills"
            discovered = list_skills(
                built_in_skills_dir=built_in_dir,
                user_skills_dir=settings.get_user_skills_dir("opscloud"),
                project_skills_dir=settings.get_project_skills_dir(),
                include_plugins=True,
                project_root=settings.project_root,
            )
            for skill_item in discovered:
                skill_dict = dict(skill_item)
                skill_name = str(skill_dict.get("name") or "").lower()
                if skill_name == name or (":" in skill_name and skill_name.rsplit(":", 1)[-1] == name):
                    return skill_dict
        except Exception as exc:
            logger.warning("Failed fallback skill discovery for %s: %s", name, exc)

        return None

    def _load_skill_content(self, skill: dict) -> str | None:
        """Read the SKILL.md file for a skill."""
        from pathlib import Path

        skill_path = skill.get("path", "")
        if not skill_path:
            return None

        skill_md = Path(skill_path)
        if not skill_md.is_file():
            # Try SKILL.md in the directory
            skill_md = Path(skill_path) / "SKILL.md"

        if not skill_md.is_file():
            return None

        try:
            from opscloud.skills.loader import load_skill_content

            return load_skill_content(str(skill_md))
        except Exception:
            try:
                return skill_md.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return None

    def _check_trust(self, ctx: CommandContext, skill: dict) -> bool:
        """Check if a skill is trusted (built-in and user skills are always trusted)."""
        source = skill.get("source", "")
        # Built-in and user skills are always trusted
        if source in {"built-in", "user"}:
            return True

        # Check trust store for project/plugin skills
        try:
            from opscloud.skills.trust import SkillTrustStore

            store = SkillTrustStore()
            from pathlib import Path

            return store.is_trusted(skill.get("name", ""), Path(skill.get("path", "")))
        except Exception:
            return True  # Default to trusted if trust store unavailable
