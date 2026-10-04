"""``/review`` — Code and IaC review with severity classification."""

from __future__ import annotations

from opscloud.utils.logger import get_logger
import subprocess

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel

logger = get_logger(__name__)

# Severity levels for review findings
SEVERITY_ORDER = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")

REVIEW_SYSTEM_PROMPT = """\
You are a senior code reviewer specialising in DevOps infrastructure.
Perform a thorough, read-only review of the diff below.

Classify every finding using one of these severity levels:
  CRITICAL — Security vulnerability, data loss risk, or production outage risk
  HIGH     — Correctness bug, missing error handling, or resource leak
  MEDIUM   — Logic concern, performance issue, or weak pattern
  LOW      — Style improvement, naming, or minor refactor opportunity
  INFO     — Observation, documentation suggestion, or praise

For Infrastructure-as-Code (Terraform, Helm, K8s, Ansible, Docker):
  - Flag hardcoded secrets or credentials
  - Check provider/version constraints
  - Verify resource naming conventions
  - Note missing input validation or outputs

Output format:
  ### <SEVERITY>: <one-line title>
  **File:** `<path>` (lines N–M)
  <explanation>

End with a brief **Summary** section.
"""


class ReviewHandler(BaseCommandHandler):
    """Perform a read-only code review of recent changes or a specific path.

    Usage:
      ``/review``          — review ``git diff HEAD`` (staged + unstaged)
      ``/review <path>``   — review a specific file or directory
      ``/review --staged``  — review only staged changes
    """

    @property
    def name(self) -> str:
        return "/review"

    @property
    def aliases(self) -> tuple[str, ...]:
        return ("/code-review",)

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.POWER

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.READ_ONLY

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.QUEUED

    async def execute(self, ctx: CommandContext) -> CommandResult:
        args = ctx.args.strip()

        # Determine what diff to review
        staged_only = "--staged" in args
        path_arg = args.replace("--staged", "").strip() or None

        diff = _get_diff(path=path_arg, staged_only=staged_only)
        if diff is None:
            return CommandResult(
                success=False,
                message="Not inside a Git repository. `/review` requires a Git project.",
            )
        if not diff.strip():
            qualifier = "staged " if staged_only else ""
            return CommandResult(
                success=True,
                message=f"No {qualifier}changes to review. Working tree is clean.",
            )

        # Send diff to agent as a structured review request
        review_prompt = (
            f"{REVIEW_SYSTEM_PROMPT}\n\n"
            "---\n"
            f"```diff\n{diff}\n```\n"
            "---\n\n"
            "Please review the diff above and provide findings."
        )

        app = ctx.app
        if app is not None and hasattr(app, "send_agent_message"):
            try:
                await app.send_agent_message(review_prompt)
                return CommandResult(
                    success=True,
                    message=None,
                    mount_as_app_message=False,
                )
            except Exception as exc:
                logger.warning("Failed to send review to agent: %s", exc)

        # Fallback: return the prompt for manual review
        return CommandResult(
            success=True,
            message=f"**Review prompt generated** ({len(diff)} chars of diff).\n"
            "Agent is not connected — copy the diff and review manually.",
        )


def _get_diff(
    *,
    path: str | None = None,
    staged_only: bool = False,
) -> str | None:
    """Run ``git diff`` and return the output, or ``None`` if not in a repo."""
    cmd = ["git", "diff"]
    if staged_only:
        cmd.append("--cached")
    else:
        cmd.append("HEAD")

    if path:
        cmd.extend(["--", path])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            # Might not be a git repo
            if "not a git repository" in (result.stderr or "").lower():
                return None
            # Return stderr as diff content so caller can report the error
            return result.stderr or ""
        return result.stdout
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return "Error: git diff timed out after 30 seconds."
