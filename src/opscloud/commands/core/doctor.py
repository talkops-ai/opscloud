"""Diagnostic health-check command handler for OpsCode.

Inspired by `claude doctor`, reports install health and diagnostics in a
tree-style grouped report. Covers: version/platform, active model, configuration
paths, API keys, MCP server connectivity, DevOps tool availability, agent
definitions, skills discovery, and workspace info.
"""

from __future__ import annotations

from opscloud.utils.logger import get_logger
import os
import platform
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel

logger = get_logger(__name__)


@dataclass
class DiagnosticItem:
    """A single diagnostic fact with health status."""
    label: str
    value: str
    ok: bool = True
    tip: str | None = None  # Remediation hint shown when ok=False


@dataclass
class DiagnosticSection:
    """A named group of diagnostic items."""
    title: str
    items: list[DiagnosticItem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(item.ok for item in self.items)


class DoctorHandler(BaseCommandHandler):
    """Handler for /doctor — comprehensive environment health check.

    Sections (matching claude doctor structure):
    1. Diagnostics — version, platform, Python, install method, path
    2. Active Model — model name, provider, effort, context limit
    3. Configuration — data directory, config file existence
    4. API Keys — LLM provider credentials status
    5. MCP Servers — connection status per server
    6. DevOps Tools — terraform, kubectl, helm, docker, cloud CLIs
    7. Agent Definitions — AGENTS.md files, collisions
    8. Skills & Plugins — discovered skills, unused/broken entries
    9. Workspace — project root, size, git status
    """

    @property
    def name(self) -> str:
        return "/doctor"

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.CORE

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.READ_ONLY

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.QUEUED

    async def execute(self, ctx: CommandContext) -> CommandResult:
        sections: list[DiagnosticSection] = []
        sections.append(self._collect_diagnostics(ctx))
        sections.append(self._collect_active_model(ctx))
        sections.append(self._collect_configuration(ctx))
        sections.append(self._collect_api_keys(ctx))
        sections.append(await self._collect_mcp(ctx))
        sections.append(self._collect_devops_tools())
        sections.append(self._collect_agents(ctx))
        sections.append(self._collect_skills(ctx))
        sections.append(self._collect_workspace(ctx))

        return CommandResult(
            success=True,
            message=self._render_tree(sections),
        )

    # ── Section collectors ──────────────────────────────

    def _collect_diagnostics(self, ctx: CommandContext) -> DiagnosticSection:
        """Version, platform, Python, install path."""
        from opscloud._version import __version__

        items: list[DiagnosticItem] = [
            DiagnosticItem("opscloud", __version__),
            DiagnosticItem(
                "Python",
                f"{platform.python_version()} ({sys.executable})",
                ok=sys.version_info >= (3, 11),
                tip="Python 3.11+ is required" if sys.version_info < (3, 11) else None,
            ),
            DiagnosticItem("Platform", f"{platform.system()}-{platform.machine()}".lower()),
            DiagnosticItem("Install path", sys.prefix),
        ]

        # Install method detection
        is_editable = self._detect_editable_install()
        items.append(DiagnosticItem(
            "Install method",
            "editable (pip install -e)" if is_editable else "package",
        ))

        # Commit hash (if in a git repo)
        commit = self._get_commit_hash()
        if commit:
            items.append(DiagnosticItem("Commit hash", commit))

        return DiagnosticSection(title="Diagnostics", items=items)

    def _collect_active_model(self, ctx: CommandContext) -> DiagnosticSection:
        """Current model, provider, effort level, context limit."""
        items: list[DiagnosticItem] = []

        model_name = getattr(ctx.settings, "model_name", None) if ctx.settings else None
        provider = getattr(ctx.settings, "model_provider", None) if ctx.settings else None
        effort = getattr(ctx.settings, "reasoning_effort", None) if ctx.settings else None
        context_limit = getattr(ctx.settings, "model_context_limit", None) if ctx.settings else None

        # Guard against MagicMock/non-str values from test mocks
        if not isinstance(model_name, str):
            model_name = None
        if not isinstance(provider, str):
            provider = None
        if not isinstance(effort, str):
            effort = None
        if not isinstance(context_limit, int):
            context_limit = None

        # Also check app._model as fallback
        model_spec = ctx.model_spec or (getattr(ctx.app, "_model", None) if ctx.app else None)

        if model_name:
            items.append(DiagnosticItem("Model", model_name))
        elif model_spec:
            items.append(DiagnosticItem("Model", str(model_spec)))
        else:
            items.append(DiagnosticItem("Model", "not configured", ok=False,
                                        tip="Use /model to select a model"))

        items.append(DiagnosticItem("Provider", provider or "not set"))
        items.append(DiagnosticItem("Reasoning effort", effort or "default"))

        if context_limit:
            from opscloud.model.config import format_token_count
            items.append(DiagnosticItem("Context window", format_token_count(context_limit)))
        else:
            items.append(DiagnosticItem("Context window", "unknown"))

        return DiagnosticSection(title="Active Model", items=items)

    def _collect_configuration(self, ctx: CommandContext) -> DiagnosticSection:
        """Check data directory and config file existence."""
        from opscloud.config import paths as config_paths
        from opscloud.config.paths import classify_path, PathState

        items: list[DiagnosticItem] = []

        # Data directory
        state = classify_path(config_paths.DATA_DIR)
        items.append(DiagnosticItem(
            "Data directory",
            f"{config_paths.DATA_DIR} ({state})",
            ok=state != PathState.UNREADABLE,
        ))

        # State directory
        state = classify_path(config_paths.STATE_DIR)
        items.append(DiagnosticItem(
            "State directory",
            f"{config_paths.STATE_DIR} ({state})",
            ok=state != PathState.UNREADABLE,
        ))

        # Config file
        cfg_state = classify_path(config_paths.CONFIG_PATH)
        items.append(DiagnosticItem(
            "Config file",
            f"{config_paths.CONFIG_PATH} ({cfg_state})",
            ok=True,
        ))

        # Validate TOML syntax if config exists
        if cfg_state == PathState.EXISTS:
            try:
                import tomllib
                with open(config_paths.CONFIG_PATH, "rb") as f:
                    tomllib.load(f)
                items.append(DiagnosticItem("Config syntax", "valid TOML"))
            except Exception as e:
                items.append(DiagnosticItem(
                    "Config syntax", f"PARSE ERROR: {e}", ok=False,
                    tip=f"Fix syntax in {config_paths.CONFIG_PATH}",
                ))

        # Standard config files
        config_files = [
            (".env", config_paths.GLOBAL_ENV_PATH),
            ("hooks.json", config_paths.HOOKS_PATH),
            (".mcp.json", config_paths.GLOBAL_MCP_PATH),
            ("auth.json", config_paths.AUTH_PATH),
        ]
        for name, path in config_files:
            fstate = classify_path(path)
            if fstate == PathState.EXISTS:
                items.append(DiagnosticItem(name, f"{path} ({fstate})"))

        return DiagnosticSection(title="Configuration", items=items)

    def _collect_api_keys(self, ctx: CommandContext) -> DiagnosticSection:
        """Check LLM provider API key availability."""
        items: list[DiagnosticItem] = []
        if ctx.settings is None:
            items.append(DiagnosticItem("Settings", "not loaded", ok=False))
            return DiagnosticSection(title="API Keys", items=items)

        key_checks = [
            ("OpenAI", "openai_api_key"),
            ("Anthropic", "anthropic_api_key"),
            ("Google", "google_api_key"),
            ("Groq", "groq_api_key"),
            ("DeepSeek", "deepseek_api_key"),
            ("Tavily", "tavily_api_key"),
            ("TypeSafe AI (Jev)", "typesafe_api_key"),
        ]
        configured_count = 0
        for label, attr in key_checks:
            has_key = bool(getattr(ctx.settings, attr, None))
            if has_key:
                configured_count += 1
            items.append(DiagnosticItem(
                label,
                "configured ✓" if has_key else "not set",
                ok=True,  # Missing keys are informational
            ))

        if configured_count == 0:
            items.insert(0, DiagnosticItem(
                "Status", "NO PROVIDERS CONFIGURED", ok=False,
                tip="Run /login to configure at least one API key",
            ))

        return DiagnosticSection(title="API Keys", items=items)

    async def _collect_mcp(self, ctx: CommandContext) -> DiagnosticSection:
        """Check MCP server connectivity."""
        items: list[DiagnosticItem] = []
        if ctx.app is None or not hasattr(ctx.app, "get_mcp_servers"):
            items.append(DiagnosticItem("Status", "MCP not available in this context"))
            return DiagnosticSection(title="MCP Servers", items=items)

        servers = ctx.app.get_mcp_servers()
        if not servers:
            items.append(DiagnosticItem("Status", "no MCP servers configured"))
            return DiagnosticSection(title="MCP Servers", items=items)

        connected_count = 0
        for srv in servers:
            name = getattr(srv, "name", "unknown")
            connected = getattr(srv, "connected", False)
            tool_count = getattr(srv, "tool_count", 0)
            if connected:
                connected_count += 1
                status = f"connected ({tool_count} tools)"
            else:
                status = "disconnected"
            items.append(DiagnosticItem(
                name, status, ok=connected,
                tip=f"Check MCP server config for '{name}'" if not connected else None,
            ))

        items.insert(0, DiagnosticItem(
            "Status", f"{connected_count}/{len(servers)} servers connected",
        ))

        return DiagnosticSection(title="MCP Servers", items=items)

    def _collect_devops_tools(self) -> DiagnosticSection:
        """Check DevOps CLI tool availability on PATH."""
        tools = [
            ("terraform", [("tofu", None)]),
            ("kubectl", []),
            ("helm", []),
            ("docker", []),
            ("aws", []),
            ("gcloud", []),
            ("az", []),
            ("argocd", []),
            ("ansible", []),
        ]
        items: list[DiagnosticItem] = []
        found_count = 0

        for tool, alternates in tools:
            path = shutil.which(tool)
            used_name = tool
            if not path and alternates:
                for alt_name, _ in alternates:
                    path = shutil.which(alt_name)
                    if path:
                        used_name = alt_name
                        break
            if path:
                found_count += 1
                items.append(DiagnosticItem(used_name, f"found at {path}"))
            else:
                items.append(DiagnosticItem(tool, "not found", ok=True))

        items.insert(0, DiagnosticItem(
            "Status", f"{found_count}/{len(tools)} tools found on PATH",
        ))

        return DiagnosticSection(title="DevOps Tools", items=items)

    def _collect_agents(self, ctx: CommandContext) -> DiagnosticSection:
        """Check agent definitions for collisions and broken configs."""
        items: list[DiagnosticItem] = []
        if ctx.settings is None:
            items.append(DiagnosticItem("Settings", "not loaded", ok=False))
            return DiagnosticSection(title="Agent Definitions", items=items)

        from opscloud.config.paths import user_agent_md, DEFAULT_AGENT_NAME
        global_md = user_agent_md(DEFAULT_AGENT_NAME)
        items.append(DiagnosticItem(
            "Global AGENTS.md",
            f"{'exists' if global_md.exists() else 'not created'}",
        ))

        project_mds = ctx.settings.get_project_agent_md_path() if hasattr(ctx.settings, "get_project_agent_md_path") else []
        if project_mds:
            for p in project_mds:
                items.append(DiagnosticItem("Project AGENTS.md", str(p)))
        else:
            items.append(DiagnosticItem("Project AGENTS.md", "none found"))

        if len(project_mds) > 1:
            items.append(DiagnosticItem(
                "Collision warning",
                f"{len(project_mds)} project AGENTS.md files — potential conflicts",
                ok=False,
                tip="Consolidate to a single AGENTS.md per project",
            ))

        return DiagnosticSection(title="Agent Definitions", items=items)

    def _collect_skills(self, ctx: CommandContext) -> DiagnosticSection:
        """Check discovered skills and plugins for issues."""
        items: list[DiagnosticItem] = []

        if ctx.settings:
            if hasattr(ctx.settings, "get_user_skills_dir"):
                user_skills = ctx.settings.get_user_skills_dir()
                items.append(DiagnosticItem(
                    "User skills dir",
                    f"{'exists' if user_skills.exists() else 'not created'}",
                ))

            if hasattr(ctx.settings, "get_project_skills_dir"):
                project_skills = ctx.settings.get_project_skills_dir()
                if project_skills:
                    items.append(DiagnosticItem(
                        "Project skills dir",
                        f"{'exists' if project_skills.exists() else 'not created'}",
                    ))

        if ctx.app and hasattr(ctx.app, "get_discovered_skills"):
            skills = ctx.app.get_discovered_skills()
            items.append(DiagnosticItem("Discovered skills", str(len(skills))))
        else:
            items.append(DiagnosticItem("Discovered skills", "discovery not available"))

        return DiagnosticSection(title="Skills & Plugins", items=items)

    def _collect_workspace(self, ctx: CommandContext) -> DiagnosticSection:
        """Workspace and project root information."""
        items: list[DiagnosticItem] = []

        cwd = Path.cwd()
        items.append(DiagnosticItem("Working directory", str(cwd)))

        # Project root
        if ctx.settings and hasattr(ctx.settings, "project_root") and ctx.settings.project_root:
            items.append(DiagnosticItem("Project root", str(ctx.settings.project_root)))
        else:
            items.append(DiagnosticItem("Project root", "not detected"))

        # Git status
        git = shutil.which("git")
        if git:
            import subprocess
            try:
                result = subprocess.run(
                    [git, "rev-parse", "--is-inside-work-tree"],
                    capture_output=True, text=True, timeout=2, check=False, cwd=cwd,
                )
                if result.returncode == 0:
                    # Get branch name
                    branch_result = subprocess.run(
                        [git, "rev-parse", "--abbrev-ref", "HEAD"],
                        capture_output=True, text=True, timeout=2, check=False, cwd=cwd,
                    )
                    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "unknown"

                    # Get dirty state
                    status_result = subprocess.run(
                        [git, "status", "--porcelain"],
                        capture_output=True, text=True, timeout=2, check=False, cwd=cwd,
                    )
                    dirty = bool(status_result.stdout.strip()) if status_result.returncode == 0 else False
                    dirty_tag = " (dirty)" if dirty else ""

                    items.append(DiagnosticItem("Git", f"branch: {branch}{dirty_tag}"))
                else:
                    items.append(DiagnosticItem("Git", "not a git repository"))
            except Exception:
                items.append(DiagnosticItem("Git", "error checking status"))

        # Shell
        items.append(DiagnosticItem("Shell", os.environ.get("SHELL", "unknown")))

        # Terminal
        term = os.environ.get("TERM_PROGRAM") or os.environ.get("TERM") or "unknown"
        items.append(DiagnosticItem("Terminal", term))

        return DiagnosticSection(title="Workspace", items=items)

    # ── Helpers ──────────────────────────────────────────

    @staticmethod
    def _detect_editable_install() -> bool:
        """Check if opscloud is installed in editable/dev mode."""
        try:
            from importlib.metadata import distribution
            dist = distribution("opscloud")
            # Editable installs have a direct_url.json with editable=true
            direct_url = dist.read_text("direct_url.json")
            if direct_url and '"editable": true' in direct_url:
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def _get_commit_hash() -> str | None:
        """Get the git commit hash for the opscloud install."""
        git = shutil.which("git")
        if not git:
            return None
        import subprocess
        try:
            repo_root = Path.cwd()
            for p in [Path.cwd(), *Path(__file__).resolve().parents]:
                if (p / ".git").exists():
                    repo_root = p
                    break
            result = subprocess.run(
                [git, "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=2, check=False,
                cwd=repo_root,
            )
            return result.stdout.strip() if result.returncode == 0 else None
        except Exception:
            return None

    # ── Tree renderer ───────────────────────────────────

    def _render_tree(self, sections: list[DiagnosticSection]) -> str:
        """Render sections as a tree-style report.

        Uses a fenced code block for the diagnostic tree so markdown rendering
        preserves the ├/└ connectors and line-per-item layout.
        """
        from opscloud.config.settings import get_glyphs
        glyphs = get_glyphs()

        all_healthy = all(s.ok for s in sections)
        issues: list[str] = []

        # Build the tree inside a code block to preserve formatting
        tree_lines: list[str] = []
        for section in sections:
            status = glyphs.checkmark if section.ok else "⚠"
            tree_lines.append(f"  {section.title} {status}")

            for i, item in enumerate(section.items):
                is_last = i == len(section.items) - 1
                connector = "└" if is_last else "├"
                warning = " ⚠" if not item.ok else ""
                tree_lines.append(f"    {connector} {item.label}: {item.value}{warning}")
                if not item.ok and item.tip:
                    issues.append(f"→ {item.tip}")

            tree_lines.append("")  # Blank line between sections

        # Summary line inside the code block
        if all_healthy:
            tree_lines.append("  Summary: All checks passed ✓")
        else:
            fail_count = sum(1 for s in sections if not s.ok)
            tree_lines.append(f"  Summary: {fail_count} section(s) need attention ⚠")

        # Remediation tips inside code block
        if issues:
            tree_lines.append("")
            tree_lines.append("  Suggested fixes:")
            for tip in issues:
                tree_lines.append(f"    {tip}")

        # Assemble final output: markdown header + code block + markdown footer
        parts: list[str] = [
            "**OpsCode Doctor**\n",
            "```",
            "\n".join(tree_lines),
            "```",
            "",
            "_Tip: `/config` to inspect settings · `/config path` for file locations · `/login` to configure API keys_",
        ]
        return "\n".join(parts)


__all__ = ["DoctorHandler"]
