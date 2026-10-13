"""DevOps-specific terminal output renderers for Terraform, Helm, Kubectl, and Ansible."""

from __future__ import annotations

import json
import re
from typing import Any

from textual.content import Content
from textual.widget import Widget
from textual.widgets import Static

from opscloud.ui.theme import ThemeColors, get_theme_colors

# ── Kubectl Status Regexes ──────────────────────────────────────────
_K8S_HEADER_RE = re.compile(
    r"\bNAME\b.*\b(READY|STATUS|RESTARTS|AGE|TYPE|CLUSTER-IP|EXTERNAL-IP|PORT\(S\)|NAMESPACE|NODE|NOMINATED)\b",
    re.IGNORECASE,
)
_K8S_GREEN_RE = re.compile(
    r"\b(Running|Ready|Completed|Succeeded|Active)\b",
    re.IGNORECASE,
)
_K8S_YELLOW_RE = re.compile(
    r"\b(Pending|ContainerCreating|Terminating|ScalingUp|ScalingDown|PodInitializing|Init:\d+/\d+)\b",
    re.IGNORECASE,
)
_K8S_RED_RE = re.compile(
    r"\b(CrashLoopBackOff|Error|OOMKilled|ImagePullBackOff|ErrImagePull|Failed|Evicted|Unknown)\b",
    re.IGNORECASE,
)

# ── Terraform Plan Regexes ──────────────────────────────────────────
_TF_PLAN_SUMMARY_RE = re.compile(
    r"Plan:\s*([+~-]?\d+)\s+to add,\s*([+~-]?\d+)\s+to change,\s*([+~-]?\d+)\s+to destroy",
    re.IGNORECASE,
)


class KubectlRenderer:
    """Render kubectl output with resource status and table header coloring."""

    def render_line(self, line: str, colors: ThemeColors | None = None) -> Content:
        """Render a single line of kubectl output with semantic highlights."""
        if not line:
            return Content("")

        # Check for table header row
        if _K8S_HEADER_RE.search(line):
            return Content.styled(line, "dim bold")

        active_colors = colors or get_theme_colors(None)

        # Collect all status matches with their span ranges and styles
        matches: list[tuple[int, int, str]] = []

        for m in _K8S_GREEN_RE.finditer(line):
            matches.append((m.start(), m.end(), f"bold {active_colors.success}"))
        for m in _K8S_YELLOW_RE.finditer(line):
            matches.append((m.start(), m.end(), f"bold {active_colors.warning}"))
        for m in _K8S_RED_RE.finditer(line):
            matches.append((m.start(), m.end(), f"bold {active_colors.error}"))

        if not matches:
            return Content(line)

        # Sort matches chronologically by start offset
        matches.sort(key=lambda x: x[0])

        pieces: list[Content] = []
        last_idx = 0
        for start, end, style in matches:
            if start < last_idx:
                continue  # overlap guard
            if start > last_idx:
                pieces.append(Content(line[last_idx:start]))
            pieces.append(Content.styled(line[start:end], style))
            last_idx = end

        if last_idx < len(line):
            pieces.append(Content(line[last_idx:]))

        return Content.assemble(*pieces)

    def render_lines(
        self, lines: list[str], colors: ThemeColors | None = None
    ) -> list[Content]:
        """Render multiple lines of kubectl output."""
        active_colors = colors or get_theme_colors(None)
        return [self.render_line(line, active_colors) for line in lines]

    def render(self, output: str, colors: ThemeColors | None = None) -> Content:
        """Render full kubectl output text as Content."""
        active_colors = colors or get_theme_colors(None)
        lines = output.splitlines()
        rendered_lines = self.render_lines(lines, active_colors)
        return Content("\n").join(rendered_lines) if rendered_lines else Content("")


class TerraformPlanRenderer:
    """Render terraform plan output with add/change/destroy highlighting."""

    def render_line(self, line: str, colors: ThemeColors | None = None) -> Content:
        """Render a single line of terraform plan output."""
        if not line:
            return Content("")

        active_colors = colors or get_theme_colors(None)
        line_stripped = line.strip()

        # Summary line check: e.g. "Plan: 3 to add, 1 to change, 0 to destroy."
        summary_match = _TF_PLAN_SUMMARY_RE.search(line)
        if summary_match:
            adds, changes, destroys = summary_match.groups()
            # Normalize format to +X, ~Y, -Z
            add_str = f"+{adds}" if not adds.startswith("+") else adds
            change_str = f"~{changes}" if not changes.startswith("~") else changes
            destroy_str = f"-{destroys}" if not destroys.startswith("-") else destroys

            destroy_count = int(destroys.lstrip("-+~")) if destroys.lstrip("-+~").isdigit() else 0
            destroy_style = f"bold {active_colors.error}" if destroy_count > 0 else f"bold {active_colors.error}"

            prefix = line[: summary_match.start()]
            suffix = line[summary_match.end() :]

            pieces: list[Content] = []
            if prefix:
                pieces.append(Content(prefix))
            pieces.extend([
                Content.styled("Plan: ", "bold"),
                Content.styled(f"{add_str} to add", f"bold {active_colors.plan_add}"),
                Content(", "),
                Content.styled(f"{change_str} to change", f"bold {active_colors.plan_change}"),
                Content(", "),
                Content.styled(f"{destroy_str} to destroy", destroy_style),
            ])
            if suffix:
                pieces.append(Content(suffix))
            return Content.assemble(*pieces)

        # Plan actions header
        if "Terraform will perform the following actions:" in line:
            return Content.styled(line, "bold")

        # Resource changes and diff lines
        if line_stripped.startswith("+") and not line_stripped.startswith("+++"):
            return Content.styled(line, active_colors.plan_add)
        if line_stripped.startswith("-") and not line_stripped.startswith("---"):
            return Content.styled(line, active_colors.plan_destroy)
        if line_stripped.startswith("~") or line_stripped.startswith("!~"):
            return Content.styled(line, active_colors.plan_change)
        if "# (create)" in line:
            return Content.styled(line, active_colors.plan_add)
        if "# (destroy)" in line:
            return Content.styled(line, active_colors.plan_destroy)
        if "# (update)" in line:
            return Content.styled(line, active_colors.plan_change)

        return Content(line)

    def render_lines(
        self, lines: list[str], colors: ThemeColors | None = None
    ) -> list[Content]:
        """Render multiple lines of terraform plan output."""
        active_colors = colors or get_theme_colors(None)
        return [self.render_line(line, active_colors) for line in lines]

    def render(self, output: str, colors: ThemeColors | None = None) -> Content:
        """Render full terraform plan output text as Content."""
        active_colors = colors or get_theme_colors(None)
        lines = output.splitlines()
        rendered_lines = self.render_lines(lines, active_colors)
        return Content("\n").join(rendered_lines) if rendered_lines else Content("")

    def render_json(
        self, plan_data: dict[str, Any], colors: ThemeColors | None = None
    ) -> Content:
        """Render structured terraform show -json plan data."""
        active_colors = colors or get_theme_colors(None)
        resource_changes = plan_data.get("resource_changes", [])
        add_cnt = sum(
            1 for r in resource_changes if "create" in r.get("change", {}).get("actions", [])
        )
        change_cnt = sum(
            1 for r in resource_changes if "update" in r.get("change", {}).get("actions", [])
        )
        delete_cnt = sum(
            1 for r in resource_changes if "delete" in r.get("change", {}).get("actions", [])
        )

        destroy_style = (
            f"bold {active_colors.error}" if delete_cnt > 0 else f"bold {active_colors.error}"
        )

        pieces: list[Content] = [
            Content.styled("Terraform Plan Summary: ", "bold"),
            Content.styled(f"+{add_cnt} to add", f"bold {active_colors.plan_add}"),
            Content(", "),
            Content.styled(f"~{change_cnt} to change", f"bold {active_colors.plan_change}"),
            Content(", "),
            Content.styled(f"-{delete_cnt} to destroy", destroy_style),
            Content("\n\n"),
        ]

        for r in resource_changes:
            address = r.get("address", "resource")
            actions = r.get("change", {}).get("actions", [])
            if "create" in actions:
                pieces.append(
                    Content.styled(f"  + {address} (create)\n", active_colors.plan_add)
                )
            elif "delete" in actions:
                pieces.append(
                    Content.styled(f"  - {address} (destroy)\n", active_colors.plan_destroy)
                )
            elif "update" in actions:
                pieces.append(
                    Content.styled(f"  ~ {address} (update)\n", active_colors.plan_change)
                )

        return Content.assemble(*pieces)


class HelmDiffRenderer:
    """Render helm diff with chart value change highlights."""

    def render_line(self, line: str, colors: ThemeColors | None = None) -> Content:
        """Render a single line of helm diff output."""
        if not line:
            return Content("")

        active_colors = colors or get_theme_colors(None)
        line_stripped = line.strip()

        if line_stripped.startswith("+") and not line_stripped.startswith("+++"):
            return Content.styled(line, active_colors.success)
        if line_stripped.startswith("-") and not line_stripped.startswith("---"):
            return Content.styled(line, active_colors.error)
        if line_stripped.startswith("@@"):
            return Content.styled(line, f"bold {active_colors.secondary}")
        if line_stripped.startswith("+++") or line_stripped.startswith("---"):
            return Content.styled(line, "bold")

        return Content(line)

    def render_lines(
        self, lines: list[str], colors: ThemeColors | None = None
    ) -> list[Content]:
        """Render multiple lines of helm diff output."""
        active_colors = colors or get_theme_colors(None)
        return [self.render_line(line, active_colors) for line in lines]

    def render(self, output: str, colors: ThemeColors | None = None) -> Content:
        """Render full helm diff output text as Content."""
        active_colors = colors or get_theme_colors(None)
        lines = output.splitlines()
        rendered_lines = self.render_lines(lines, active_colors)
        return Content("\n").join(rendered_lines) if rendered_lines else Content("")


class AnsiblePlaybookRenderer:
    """Render ansible playbook task execution output."""

    def render_line(self, line: str, colors: ThemeColors | None = None) -> Content:
        """Render a single line of ansible playbook output."""
        if not line:
            return Content("")

        active_colors = colors or get_theme_colors(None)

        if "TASK [" in line or "PLAY [" in line:
            return Content.styled(line, f"bold {active_colors.primary}")
        if "ok:" in line or "ok: [" in line:
            return Content.styled(line, active_colors.success)
        if "changed:" in line or "changed: [" in line:
            return Content.styled(line, active_colors.warning)
        if "failed:" in line or "fatal:" in line:
            return Content.styled(line, f"bold {active_colors.error}")
        if "PLAY RECAP" in line:
            return Content.styled(line, f"bold {active_colors.secondary}")
        if "skipping:" in line:
            return Content.styled(line, active_colors.muted)

        return Content(line)

    def render_lines(
        self, lines: list[str], colors: ThemeColors | None = None
    ) -> list[Content]:
        """Render multiple lines of ansible output."""
        active_colors = colors or get_theme_colors(None)
        return [self.render_line(line, active_colors) for line in lines]

    def render(self, output: str, colors: ThemeColors | None = None) -> Content:
        """Render full ansible output text as Content."""
        active_colors = colors or get_theme_colors(None)
        lines = output.splitlines()
        rendered_lines = self.render_lines(lines, active_colors)
        return Content("\n").join(rendered_lines) if rendered_lines else Content("")


def get_devops_renderer_for_command(
    command: str,
) -> KubectlRenderer | TerraformPlanRenderer | HelmDiffRenderer | AnsiblePlaybookRenderer | None:
    """Inspect shell command string and return specialized DevOps renderer if matched."""
    if not command:
        return None

    clean = command.strip()
    if clean.startswith("$ "):
        clean = clean[2:].strip()

    # Handle sudo or env var prefixes
    while clean.startswith("sudo "):
        clean = clean[5:].strip()

    # Kubectl checks
    if clean.startswith("kubectl ") or clean == "kubectl" or clean.startswith("k ") or clean == "k":
        return KubectlRenderer()

    # Terraform checks
    if "terraform plan" in clean or "tf plan" in clean:
        return TerraformPlanRenderer()

    # Helm diff checks
    if clean.startswith("helm diff"):
        return HelmDiffRenderer()

    # Ansible checks
    if clean.startswith("ansible-playbook") or clean.startswith("ansible "):
        return AnsiblePlaybookRenderer()

    return None


def detect_devops_renderer(
    output: str, command: str = ""
) -> KubectlRenderer | TerraformPlanRenderer | HelmDiffRenderer | AnsiblePlaybookRenderer | None:
    """Detect renderer from command or by inspecting initial lines of output."""
    renderer = get_devops_renderer_for_command(command)
    if renderer is not None:
        return renderer

    if not output:
        return None

    sample_lines = [line.strip() for line in output.splitlines()[:5] if line.strip()]
    for line in sample_lines:
        if _K8S_HEADER_RE.search(line):
            return KubectlRenderer()
        if _TF_PLAN_SUMMARY_RE.search(line) or "Terraform will perform the following actions:" in line:
            return TerraformPlanRenderer()
        if "PLAY [" in line or "TASK [" in line:
            return AnsiblePlaybookRenderer()

    return None


class TerraformPlanWidget(Widget):
    """Widget container displaying formatted Terraform plan outputs."""

    DEFAULT_CSS = """
    TerraformPlanWidget {
        padding: 1;
        margin: 1 0;
        background: $background;
        border: solid $panel;
    }
    """

    def __init__(self, plan_output: str, is_json: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._plan_output = plan_output
        self._is_json = is_json

    def compose(self):
        renderer = TerraformPlanRenderer()
        colors = get_theme_colors(self) if self.is_attached else get_theme_colors(None)
        if self._is_json:
            try:
                data = json.loads(self._plan_output)
                rendered = renderer.render_json(data, colors=colors)
            except Exception:
                rendered = renderer.render(self._plan_output, colors=colors)
        else:
            rendered = renderer.render(self._plan_output, colors=colors)

        yield Static("Terraform Plan Output", classes="title")
        yield Static(rendered)


__all__ = [
    "AnsiblePlaybookRenderer",
    "HelmDiffRenderer",
    "KubectlRenderer",
    "TerraformPlanRenderer",
    "TerraformPlanWidget",
    "detect_devops_renderer",
    "get_devops_renderer_for_command",
]
